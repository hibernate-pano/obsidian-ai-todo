# AI 代办 → Obsidian

在日记里写待办，agent 自动执行，结果回写到同一行。写完就走开。

**v0.1.0** · 纯 Python 标准库，只依赖本机的 `claude` CLI。

## 怎么用

在任意日记里加一节：

```markdown
## AI 代办

- [ ] 统计本周各项目的 commit 数
- [ ] 汇总近 7 天 agent 会话时长
```

然后跑：

```bash
# 先看分类，不真跑
python3 runner.py --vault <库路径> --dry-run

# 跑一遍就退出
python3 runner.py --vault <库路径> --once

# 常驻监听：日记一改，3 秒内自动捡任务
python3 runner.py --vault <库路径> --watch
```

结果回写：

```markdown
- [x] 统计本周各项目的 commit 数 → 53 个 commit / 11 个项目
- [!] 删除 .trash 里的文件 → 需要你确认才能做（含「删」）
```

三种状态标记：

| 标记 | 含义 |
|---|---|
| `- [ ]` | 待执行，下次跑会捡起来 |
| `- [x]` | 已完成，结果在 `→` 后面 |
| `- [!]` | 卡住了，需要你看一眼（`→` 后面写了原因） |

## 安全设计

**1. 库必须显式指定，且默认保护你正在用的那个**

```bash
# 不给库直接报错，绝不会默认写任何库
python3 runner.py --once
# ✗ the following arguments are required: --vault

# Obsidian 当前打开着的库默认受保护
python3 runner.py --vault <主力库> --once
# ✗ 拒绝在受保护的库上运行
#   确认要动它，加 --i-know-this-is-real
```

受保护列表自动从 Obsidian 的 `obsidian.json` 读「当前打开的库」，
换机器、换库名都不用改代码。要额外保护别的库：

```bash
python3 runner.py --vault <库> --protect <另一个库> --protect <再一个>
```

**2. 危险任务不执行，转成 `[!]` 等你**

命中这些词的直接拦下：`推送 / push / 删 / rm / 轮换 / 支付 / 发布 / 上传 / 部署 / 发邮件`。
只读任务误伤（比如「统计被删的文件数」）也照拦——宁可多问你一次，不赌它真的只读。

**3. 硬禁止项，连给 agent 机会都不给**

`rm -rf`、`sudo`、`git push --force`、`DROP TABLE`、`DELETE FROM`、`curl | sh`、`chmod 777`

**4. agent 被锁在目标库里**

- prompt 里写明【本库路径】+ 明确禁止去受保护的其它库
- `subprocess` 的 `cwd` 也设成那个库

> 这条是实测踩出来的：第一版没写库路径，agent 自己去读了主力库，
> 报「793 篇日记」——而目标库实际只有 15 篇。修完之后数字全对。

**5. 幂等**

按任务文本 hash 记已执行，同一句话不会被跑第二次。`[x]` 和 `[!]` 都不会重复执行。

## 长结果怎么处理

超过 200 字的结果不塞进日记行（会把日记撑爆），只留摘要 + 指向：

```
→ 数完了…（全文见 AI 代办/输出/）
```

全文写进 `<库>/AI 代办/输出/YYYY-MM-DD-HHMM <任务摘要>.md`。

## 性能实测

| | 串行（旧） | 并行 3 路（现） |
|---|---|---|
| 4 条任务 | 8 分钟没跑完 | **125 秒** |
| 单条耗时 | 2-3 分钟 | 2-3 分钟（不变） |

超时设 10 分钟/件。真的超了标记 `[!]`，不会静默卡死。

## 状态文件

```
~/.config/ai-todo/
├── state.json    # 已执行记录（防重复）
└── runner.log    # 完整执行日志
```

换库测试时先 `rm -rf ~/.config/ai-todo`，否则同文本任务会被当成「跑过了」跳过。
也可以用 `--state-dir` 指定别的目录。

## 已知限制

1. **只认一个 heading**。默认 `## AI 代办`（`AI代办` / `AI 待办` 也认）。
2. **只处理今天（或最近一天）的日记**。不会扫全库的历史待办。
3. **不处理子任务的层级关系**。缩进的多级待办会被当成平级。
4. **agent 会真的改文件**（`--permission-mode acceptEdits`）。只读类任务安全，
   写文件类任务要你判断值不值得给它。
5. **并行 3 路意味着 3 个 claude 进程**。同时用 Claude Code 可能互相挤，
   改 `MAX_PARALLEL = 1` 可退化。
6. **依赖 `claude` CLI 在 PATH 里**。

## 文件

```
ai-todo/
├── todoqueue.py  # 解析 + 回写（纯文本操作，不调 agent）
├── runner.py     # 调度器（分类、并行、超时、状态）
├── LICENSE       # MIT
└── CHANGELOG.md
```

`todoqueue.py` 可以单独用，只解析不改：

```bash
python3 todoqueue.py <日记路径>
# 输出 JSON：每条的 line / mark / status / task / result
```
