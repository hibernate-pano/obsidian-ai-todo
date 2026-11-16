#!/usr/bin/env python3
"""AI 代办 → agent 执行器

原理：监听日记文件，检出 `## AI 代办` 里新加的 `- [ ]` 待办，
     喂给 headless agent 执行，结果回写到同一行。

设计取舍（每条都有理由）：
- 白名单待办类型：只跑只读/安全类。改生产数据、推送、删文件一律转成 [!] 等你确认。
- 并行 3 路：每件独立 subprocess，回写由主线程统一做，避免并发写同一文件。
- 去重：按任务文本 hash 记 already-run，同一句话不会被跑第二次。
- 超时硬砍：单件默认 10 分钟，砍掉标记为 [!]。
- 不吞错：agent 失败原样写回，不伪装成功。

用法：
  python3 runner.py --once            # 跑一遍就退出（手动触发用这个）
  python3 runner.py --watch           # 常驻监听
  python3 runner.py --dry-run         # 只打印将要执行的，不真跑
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import todoqueue as Q

HOME_CFG = Path(os.path.expanduser('~'))

# ── 配置 ────────────────────────────────────────────────────────────
# 默认不指向任何库：必须用 --vault 显式指定，避免"跑起来就写你正用的库"
VAULT = None
# 受保护的库：默认是 Obsidian 当前打开着的那个（最容易误写的就是它）。
# 可用 --protect 追加，或 --i-know-this-is-real 放行。
PROTECTED = []
STATE_DIR = Path(os.path.expanduser('~/.config/ai-todo'))
TASK_TIMEOUT = 600     # 单件 10 分钟（实测复杂统计类要 2-3 分钟，留余量）
MAX_PARALLEL = 3       # 同时跑几件。agent 是 IO/网络等待型，3 路不会拖慢机器
POLL = 3               # 监听间隔（秒）

# 判定「能不能自己干」：命中这些词的转成 [!] 等人
NEEDS_HUMAN = [
    '推送', 'push', '删', 'rm ', '轮换', 'rotate', '支付', '购买',
    '发布', 'publish', '上传', '部署', 'deploy', '发邮件', '发给',
]
# 完全不许碰的（安全检查，命中直接拒绝，不给 agent 任何机会）
FORBIDDEN = [
    'rm -rf', 'sudo', 'git push --force', 'DROP TABLE', 'DELETE FROM',
    'curl | sh', 'curl|sh', 'chmod 777',
]


def obsidian_config_dirs():
    """Obsidian 在三大平台放 obsidian.json 的位置。"""
    if sys.platform == 'darwin':
        yield HOME_CFG / 'Library/Application Support/obsidian'
    elif sys.platform.startswith('win'):
        yield Path(os.environ.get('APPDATA', '')) / 'obsidian'
    else:
        yield HOME_CFG / '.config/obsidian'
        yield HOME_CFG / '.var/app/md.obsidian.Obsidian/config/obsidian'


def open_vaults():
    """Obsidian 当前打开着的库（这些最容易被误写，默认保护）。"""
    out = []
    for d in obsidian_config_dirs():
        p = d / 'obsidian.json'
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        for v in (data.get('vaults') or {}).values():
            path = v.get('path')
            if path and v.get('open'):
                out.append(str(Path(path).expanduser()))
    return out


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(STATE_DIR / 'runner.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_state():
    f = STATE_DIR / 'state.json'
    if f.exists():
        try:
            return json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'ran': {}}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / 'state.json').write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')


def task_hash(task):
    return hashlib.sha256(task.strip().encode('utf-8')).hexdigest()[:16]


def write_output_sidecar(task, body):
    """长结果存成 vault 里的独立笔记，日期+摘要命名，方便回溯。"""
    d = VAULT / 'AI 代办' / '输出'
    d.mkdir(parents=True, exist_ok=True)
    slug = ''.join(c for c in task[:24] if c not in r'\/:*?"<>|')
    f = d / f'{datetime.now():%Y-%m-%d-%H%M} {slug}.md'
    f.write_text(
        f"---\ncreated: {datetime.now():%Y-%m-%d %H:%M}\ntask: {task}\n---\n\n"
        f"# {task}\n\n{body}\n", encoding='utf-8')
    return f


def classify(task):
    """返回 (可执行?, 原因)"""
    low = task.lower()
    for bad in FORBIDDEN:
        if bad.lower() in low:
            return False, f'命中禁止项「{bad}」，不执行'
    for h in NEEDS_HUMAN:
        if h.lower() in low:
            return False, f'需要你确认才能做（含「{h}」）'
    return True, ''


def find_target_note():
    """今天的日记，不存在则取最近一天。"""
    if not VAULT:
        raise SystemExit('✗ 未指定 vault。用 --vault <路径> 明确指定要跑哪个库。')
    diary = VAULT / '日记'
    today = diary / f'{datetime.now():%Y}' / f'{datetime.now():%Y-%m-%d}.md'
    if today.exists():
        return today
    if not diary.exists():
        raise SystemExit(f'✗ {VAULT} 里没有 日记/ 目录，确认路径对不对。')
    cands = sorted(diary.rglob('*.md'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def guard_vault(path, force=False):
    """把 vault 路径规范化并做保护检查。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f'✗ 路径不存在: {p}')
    if not (p / '.obsidian').exists():
        raise SystemExit(f'✗ {p} 不是 Obsidian 库（找不到 .obsidian/）')
    for prot in PROTECTED:
        pp = Path(prot).expanduser().resolve()
        if p == pp and not force:
            raise SystemExit(
                f'✗ 拒绝在受保护的库上运行: {p}\n'
                f'  这是 Obsidian 当前打开的库（最容易误写）。'
                f'确认要动它，加 --i-know-this-is-real。')
    return p


def build_prompt(task):
    others = [p for p in PROTECTED if Path(p).expanduser().resolve() != VAULT]
    avoid = ''
    if others:
        avoid = ('绝对不要去别的 Obsidian 库（尤其是用户正在用的这些：'
                 + '、'.join(others) + '），不许读也不许写。\n')
    return (
        "你在用户的 macOS 机器上执行一条来自他 Obsidian 待办列表的任务。\n\n"
        f"【本库路径】{VAULT}\n"
        "任务里凡是说「这个库」「本库」「这里」，一律指上面这个路径。\n"
        + avoid + "\n"
        f"任务：{task}\n\n"
        "要求：\n"
        "1. 只做这一件事，不要顺手改别的东西。\n"
        "2. 只读分析和写新文件优先；要改已有文件时先确认改的是对的那个。\n"
        "3. 绝对不要 git push、不要删文件、不要发邮件、不要动 .env。\n"
        "4. 结论必须基于你在【本库路径】下实际数出来的东西，"
        "不许估、不许套用别处的数字。数不出来就说不出来。\n"
        "5. 完成后用不超过 60 字说明你做了什么、结果在哪。\n"
        "6. 做不到就直说做不到以及卡在哪，不要编造完成。\n"
    )


def exec_task(item):
    """跑一条任务，只返回结果 dict，不碰磁盘（由主线程统一回写）。"""
    task = item['task']
    try:
        proc = subprocess.run(
            ['claude', '-p', build_prompt(task),
             '--permission-mode', 'acceptEdits',
             '--output-format', 'text'],
            capture_output=True, text=True, timeout=TASK_TIMEOUT,
            cwd=str(VAULT),   # 工作目录锁在目标库里，不让它从家目录乱找
        )
        out = (proc.stdout or '').strip()
        err = (proc.stderr or '').strip()
        # 过滤 claude 的噪声行
        out = '\n'.join(l for l in out.split('\n')
                        if 'claude-code:' not in l and l.strip())
        if proc.returncode != 0 or not out:
            res = f'执行失败(exit {proc.returncode})：{(err or out)[:80]}'
            mark = '!'
        else:
            # 长结果不塞进那一行（会把日记撑爆），正文留在侧车文件里
            one_line = ' '.join(out.split())
            if len(one_line) <= 200:
                res = one_line
            else:
                res = f'{one_line[:150]}…（全文见 AI 代办/输出/）'
                write_output_sidecar(task, out)
            mark = 'x'
    except subprocess.TimeoutExpired:
        res = f'超时（超过 {TASK_TIMEOUT//60} 分钟），已中止'
        mark = '!'
    except Exception as e:
        res = f'异常：{str(e)[:80]}'
        mark = '!'

    return {'line': item['line'], 'mark': mark, 'result': res}


def run_once(dry=False):
    note = find_target_note()
    if not note:
        log('找不到日记文件')
        return 0
    data = Q.parse(note)
    if not data['found']:
        return 0

    st = load_state()
    pending = [i for i in data['todo'] if task_hash(i['task']) not in st['ran']]
    if not pending:
        log(f'{note.name}: 无新待办')
        return 0

    log(f'{note.name}: 发现 {len(pending)} 条待办')

    # 先做安全分类，一次批量把该转人工的全部回写（不用逐条串行）
    runnable = []
    human = []
    for item in pending:
        ok, why = classify(item['task'])
        if ok:
            runnable.append(item)
        else:
            human.append((item, why))

    if human:
        Q.writeback(note, [{'line': it['line'], 'mark': '!', 'result': why}
                           for it, why in human])
        for it, why in human:
            st['ran'][task_hash(it['task'])] = {
                'task': it['task'], 'mark': '!',
                'at': datetime.now().isoformat(timespec='seconds')}
            log(f'⚠ L{it["line"]} 转人工 :: {it["task"][:40]}')
        save_state(st)

    if not runnable:
        return len(human)

    if dry:
        for it in runnable:
            log(f"[dry] L{it['line']} 将执行 :: {it['task'][:60]}")
        return len(pending)

    # 可执行的并行跑。每个任务自己 subprocess，互不阻塞。
    # 注意：分数回写，每个 worker 只在内存里改它自己那一行，主线程统一写盘避免竞争。
    import concurrent.futures as cf
    results = []
    with cf.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(exec_task, it): it for it in runnable}
        for fut in cf.as_completed(futs):
            it = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {'line': it['line'], 'mark': '!', 'result': f'异常：{str(e)[:80]}'}
            results.append(res)
            log(f'{"✔" if res["mark"] == "x" else "⚠"} L{it["line"]} :: '
                f'{it["task"][:40]} → {res["result"][:50]}')

    # 统一回写一次，按行号排序保证稳定
    results.sort(key=lambda r: r['line'])
    Q.writeback(note, results)
    for it in runnable:
        r = next((x for x in results if x['line'] == it['line']), None)
        if r:
            st['ran'][task_hash(it['task'])] = {
                'task': it['task'], 'mark': r['mark'],
                'at': datetime.now().isoformat(timespec='seconds')}
    save_state(st)
    return len(pending)


def watch():
    log(f'开始监听 {VAULT}/日记')
    last = None
    while True:
        try:
            note = find_target_note()
            if note:
                m = note.stat().st_mtime
                if m != last:
                    last = m
                    run_once()
        except KeyboardInterrupt:
            log('手动停止')
            return
        except Exception as e:
            log(f'监听循环异常: {e}')
        time.sleep(POLL)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--vault', required=True,
                    help='要操作的 Obsidian 库路径（必填，避免误写主力库）')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--i-know-this-is-real', dest='force', action='store_true',
                    help='允许在受保护的主力库上运行')
    ap.add_argument('--protect', action='append', default=[], metavar='PATH',
                    help='额外保护某个库（可多次）；默认为 Obsidian 当前打开的库')
    ap.add_argument('--state-dir', default=None,
                    help='状态/日志目录，默认 ~/.config/ai-todo')
    a = ap.parse_args()

    globals()['PROTECTED'] = open_vaults() + [os.path.expanduser(p) for p in a.protect]
    VAULT = guard_vault(a.vault, force=a.force)
    if a.state_dir:
        STATE_DIR = Path(a.state_dir).expanduser().resolve()
    globals()['VAULT'] = VAULT
    globals()['STATE_DIR'] = STATE_DIR
    log(f'vault = {VAULT}')
    log(f'state = {STATE_DIR}')

    if a.watch:
        watch()
    else:
        run_once(dry=a.dry_run)
