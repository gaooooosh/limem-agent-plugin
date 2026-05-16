---
name: limem.resume
description: >-
  Clear LiMem pause state immediately, restoring recall + capture. Use when
  user says "resume limem", "继续召回", "解除暂停". Calls MCP `limem_resume`.
arguments: []
---

# /limem.resume — 立即解除暂停

## 何时调用
- 用户说"恢复 LiMem"、"继续召回"、"解除暂停"
- 用户提前结束了 `/limem.pause` 设置的窗口

## 处理步骤

1. 调 MCP `limem_resume`（无参数）
2. 回执：
   ```
   ▶  LiMem 已恢复。下一次 prompt 会重新做三层召回。
   ```
