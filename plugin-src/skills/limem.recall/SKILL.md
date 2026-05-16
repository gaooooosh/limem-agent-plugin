---
name: limem.recall
description: >-
  Search LiMem long-term memory for rules/facts/notes that match a phrase. Use
  when the user asks "do I have any rules about X?" or when you want to
  double-check before doing something irreversible (e.g. running a build cmd).
arguments: [query]
---

# /limem.recall — 主动查询长期记忆

## 何时调用
- 用户问"我以前对 X 说过什么？" / "do I have a rule about Y?"
- 你（模型）准备执行某个高代价/不可逆操作前主动 check
- 用户在 `/limem.recall <query>` 显式触发

## 步骤

1. 把 $1 / $ARGUMENTS 当成 query
2. 调 MCP 工具 `limem_search`（参数：`query`, `top_k=5`）
3. 渲染结果给用户。每条至少显示：
   - event_id 前 12 位
   - type · scope · role
   - 原文 text
   - 触发命中的 pattern（若 source=pattern）
4. 若空结果，告知"未找到匹配记忆"并询问是否想用 /limem.remember 记下

## 调用语法
- Claude Code: `/limem.recall <query>`
- Codex: `$limem.recall <query>`
