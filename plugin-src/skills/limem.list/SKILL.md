---
name: limem.list
description: >-
  Show all active rules/feedback/preferences in LiMem for the current project +
  global scope, plus the active principals (user / agent / project) and which of
  them carry markdown profiles. Use when the user asks "what rules do I have
  for this project?" or you want to audit memory state.
arguments: []
---

# /limem.list — 列出当前项目的长期规则 + active principals

## 步骤
1. 调 MCP 工具 `limem_list`（默认参数：types=[rule,feedback,preference]，include_global=true）
2. 按 scope 分组渲染（project / global）；每条带 id、type、role、原文
3. 渲染 `principals` 段：列出 active principals 与其 `has_pattern` 状态
4. 末尾提示：用 `/limem.forget <id>` 修剪 / `/limem.remember` 新增 / `/limem.pattern <alias>` 编辑档案

## 输出格式
```
📚 LiMem 状态（project=<project_id>）

【项目规则】共 N 条
  • <id> [rule · forbidden] 以后这个项目不要用 npm run dev，直接 docker rebuild
  • ...

【全局规则】共 M 条
  • <id> [preference] 偏好 pnpm 而非 npm
  • ...

【Active Principals】共 K 个
  • project · <canonical>  pattern=✓
  • user    · <canonical>  pattern=·
  • agent   · codex        pattern=✓

提示：
  /limem.pattern project|user|agent get   查看档案
  /limem.pattern project|user|agent append 追加章节
```
