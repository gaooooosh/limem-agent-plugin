---
name: limem.note
description: >-
  Save a casual note or short-lived fact to LiMem. Unlike /limem.remember, this
  defaults to importance=0.3 and is NOT auto-injected at SessionStart. Use for
  TODOs, scratch ideas, ephemeral state ("this build is for benchmarking, not
  prod").
arguments: [text]
---

# /limem.note — 快速备忘

不需要 scope 交互（默认 project，可加 `--global` flag）。无需 entities（fact 类不强求 patterns）。

调用 MCP `limem_write`，参数：
- `mem_type="note"`
- `importance=0.3`
- `entities=[]`（除非用户提及了明显的命名实体）
