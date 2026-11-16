# 变更记录

## 0.1.0 — 2026-11-16

首个版本。

**可移植**
- 受保护的库不再硬编码某个人的主力库路径：默认从 Obsidian 的 `obsidian.json`
  读「当前打开的库」，可用 `--protect` 追加。
- prompt 与日志去掉个人姓名和具体库路径。
- 状态目录从 `~/.config/alma/state/ai-todo` 改为 `~/.config/ai-todo`
  （不再依赖 Alma）；`--state-dir` 仍可覆盖。
