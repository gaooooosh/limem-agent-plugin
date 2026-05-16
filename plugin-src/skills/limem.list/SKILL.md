---
name: limem.list
description: >-
  Show all active rules/feedback/preferences in LiMem for the current project +
  global scope. Use when the user asks "what rules do I have for this
  project?" or you want to audit memory state.
arguments: []
---

# /limem.list — 列出当前项目的所有长期规则

## 步骤
1. 调 MCP 工具 `limem_list`（默认参数：types=[rule,feedback,preference]，include_global=true）
2. 按 scope 分组渲染（project / global）；每条带 id、type、role、原文
3. 末尾提示：用 `/limem.forget <id>` 修剪 / `/limem.remember` 新增

## 输出格式
```
📚 LiMem 规则总览（project=<project_id>）

【项目级】共 N 条
  • <id> [rule · forbidden] 以后这个项目不要用 npm run dev，直接 docker rebuild
  • ...

【全局】共 M 条
  • <id> [preference] 偏好 pnpm 而非 npm
  • ...
```
