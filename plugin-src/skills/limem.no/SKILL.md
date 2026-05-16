---
name: limem.no
description: >-
  Silence a specific recalled memory for the current session only (not deleted).
  Use when user says "this rule is wrong for now", "本会话不要再提那条", "/limem.no
  #xxxx". Calls MCP `limem_mute`. Clears automatically at SessionEnd/Stop.
arguments: [short_id]
---

# /limem.no — 本会话静音某条记忆

## 何时调用
- 用户希望在本会话剩余轮次内不再被注入某条规则
- 不想真正删除它（下次 session 会恢复）

## 处理步骤

1. 解析 `$1` = short_id
2. 解析当前 `session_id`（从 hook payload 或 MCP context）
3. 调 MCP `limem_mute`：
   ```json
   {"short_id": "<id>", "session_id": "<sid>"}
   ```
4. 回执：
   ```
   🔇 本会话已静音 #abc123def456；新会话恢复。
   ```

## 注意
- 静音只作用于本 session_id；不写后端
- SessionEnd / Stop 自动清理
