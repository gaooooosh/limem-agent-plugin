---
name: limem.pause
description: >-
  Suspend LiMem recall + capture for a duration (default 60m). Use when user
  says "pause limem", "暂停记忆", "mute memory for an hour", or wants to do
  exploratory work without rules pulling in. Calls MCP `limem_pause`.
arguments: [duration]
---

# /limem.pause — 暂停 LiMem 召回与采集

## 何时调用
- 用户明确说"暂停 LiMem"、"关掉记忆 30 分钟"、"先别召回了"
- 用户在做大量实验性输入，不希望污染被动学习采集
- 默认 duration 为 60m；支持 `30m` / `2h` / `until-session-end`（后者传 0）

## 处理步骤

1. 解析 duration（args[0] 或 "$1"）：
   - 形如 `30m` / `2h` → 转为秒
   - `until-session-end` → 传 0
   - 缺省 → 3600（1 小时）
2. 调 MCP `limem_pause` 工具，参数：
   ```json
   {"duration_seconds": <计算后秒数>, "scope": "project", "session_id": "<当前 session 若知>"}
   ```
3. 回执：
   ```
   ⏸  LiMem 已暂停 30 分钟。期间所有 hook 不再注入记忆，也不会写 events.ndjson。
   恢复：等过期 / 或 `/limem.resume`
   ```

## 注意
- 暂停期间用户输入**完全不会**被被动学习器采集（events.ndjson 也不写）
- 暂停状态写在 `~/.cache/limem/pause.json`，daemon 死透时 hook 仍能读到
