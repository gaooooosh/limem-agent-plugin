---
name: limem.fix
description: >-
  Update a memory's text in-place via short_id (#xxxx). Use when user wants to
  refine an existing rule without creating a new event ("把那条改成 …", "fix the
  rule about npm dev"). Calls MCP `limem_fix`. Does NOT generate a new event_id.
arguments: [short_id, new_text]
---

# /limem.fix — 修订已有记忆（短 id）

## 何时调用
- 用户在召回区块看到 `#abc123def456` 后说"把那条改成 …"
- 用户希望保留 event_id 历史链但更新文本

## 处理步骤

1. 解析 `$1` = short_id（接受 `#xxxx` 或裸 hex），`$2..` = 新文本
2. 调 MCP `limem_fix`：
   ```json
   {"short_id": "<去掉 # 的 hex>", "new_text": "<新文本>"}
   ```
3. 回执：
   ```
   ✓ 已更新 LiMem event #abc123def456：
   旧文本 → 新文本
   ```

## 注意
- 这是 **update**，不是 create；event_id 不变，历史链保留
- 若 short_id 不在本地映射，工具会返回 NOT_FOUND_SHORT_ID 错误，提示用户检查 id 拼写
