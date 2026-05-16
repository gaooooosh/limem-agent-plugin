---
name: limem.feedback
description: >-
  Persist a corrective lesson tied to the previous assistant response. Use when
  user pushes back ("no, not like that" / "this is wrong because..." / "stop
  doing X"). Calls limem_write with mem_type=feedback and links the prior
  assistant turn hash as evidence.
arguments: [text]
---

# /limem.feedback — 把"用户对刚才回答的纠正"固化

与 `/limem.remember` 几乎相同，区别：
- `mem_type` 固定为 `"feedback"`
- 默认 scope=project（feedback 通常是项目内的纠正）
- 模型应在 entities 里至少给出**1 个 forbidden 实体**（描述"不要这么做的事"），尽量也给 1 个 preferred（"应该改成什么"）

调用 MCP `limem_write` 时把 `mem_type="feedback"`、`importance=0.85`、`detail` 字段填上 "对上一次回答的纠正" 提示。

## 调用语法
- Claude Code: `/limem.feedback <text>`
- Codex: `$limem.feedback <text>`
