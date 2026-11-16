#!/usr/bin/env python3
"""AI 代办队列：从 Obsidian 笔记里精确切出 `## AI 代办` 这一节，解析待办项。

设计要点：
- 只认 heading 精确切片，不做全库模糊搜索（避免误读正文里的 checkbox）
- 每个待办项带绝对行号，回写时只改那一行
- 认三种状态：`- [ ]` 待执行 / `- [x]` 已完成 / `- [!]` 卡住等你决定
"""
import re
import sys
import json
from pathlib import Path

HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
# - [ ] 文本    - [x] 文本    - [!] 文本   支持缩进、有序列表
ITEM_RE = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+\[([ xX!~])\]\s+(.*)$')

STATUS = {' ': 'todo', 'x': 'done', 'X': 'done', '!': 'blocked', '~': 'blocked'}


def _norm(text):
    return text.replace(' ', '').replace('\u3000', '').strip()


def heading_names(heading_text='AI 代办'):
    """标题的可接受写法。默认标题额外容忍「AI 待办」。"""
    base = _norm(heading_text)
    names = {base}
    if base == 'AI代办':
        names.add('AI待办')
    return names


def find_section(lines, heading_text='AI 代办'):
    """返回 (起始行号, 结束行号, 级别)。找不到返回 None。"""
    names = heading_names(heading_text)
    start = None
    level = None
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        hashes, text = m.group(1), m.group(2)
        if start is None:
            # 标题前缀匹配（容忍「AI 代办清单」这类后缀）
            norm = _norm(text)
            if any(norm.startswith(n) for n in names):
                start, level = i, len(hashes)
                continue
        else:
            # 遇到同级或更高级的 heading 就结束
            if len(hashes) <= level:
                return start, i, level
    if start is not None:
        return start, len(lines), level
    return None


def parse(path, heading_text='AI 代办'):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    lines = text.split('\n')
    sec = find_section(lines, heading_text)
    if not sec:
        return {'found': False, 'path': str(p), 'items': []}
    start, end, level = sec
    items = []
    for i in range(start + 1, end):
        m = ITEM_RE.match(lines[i])
        if not m:
            continue
        indent, mark, body = m.group(1), m.group(2), m.group(3)
        # 回写标记：`→ 结果文字` 之前的部分是任务本体
        task, _, result = body.partition(' → ')
        items.append({
            'line': i,            # 0-based 行号
            'indent': indent,
            'mark': mark,
            'status': STATUS[mark],
            'task': task.strip(),
            'result': result.strip(),
            'raw': lines[i],
        })
    return {
        'found': True,
        'path': str(p),
        'heading_line': start,
        'end_line': end,
        'items': items,
        'todo': [x for x in items if x['status'] == 'todo'],
        'done': [x for x in items if x['status'] == 'done'],
        'blocked': [x for x in items if x['status'] == 'blocked'],
    }


def writeback(path, results):
    """把执行结果写回对应行。results: [{line, mark, result}]"""
    p = Path(path)
    lines = p.read_text(encoding='utf-8').split('\n')
    changed = 0
    for r in results:
        i = r['line']
        m = ITEM_RE.match(lines[i])
        if not m:
            continue
        indent, _, body = m.group(1), m.group(2), m.group(3)
        task, _, _old = body.partition(' → ')
        bullet = lines[i].lstrip()[:1]
        if bullet not in '-*+':
            bullet = '-'
        new = f"{indent}{bullet} [{r['mark']}] {task.strip()}"
        if r.get('result'):
            new += f" → {r['result']}"
        lines[i] = new
        changed += 1
    p.write_text('\n'.join(lines), encoding='utf-8')
    return changed


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: queue.py <笔记路径>', file=sys.stderr)
        sys.exit(1)
    data = parse(sys.argv[1])
    print(json.dumps(data, ensure_ascii=False, indent=2))
