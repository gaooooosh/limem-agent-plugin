---
name: limem.forget
description: >-
  Archive a LiMem memory by its event_id, so it stops being recalled in future
  sessions. Use when the user says "forget that rule" / "cancel X" / "remove
  the memory about Y".
arguments: [event_id_or_query]
---

# /limem.forget — 撤销一条记忆

## 步骤

1. 解析 $1 / $ARGUMENTS：
   - 若像 `r_xxxx` / `<hex>_<ts>_<short>` 格式 → 直接作为 event_id
   - 否则视为模糊查询 → 先调 `limem_search` 拿匹配集 → 若 >1 条让用户挑 → 拿到唯一 event_id
2. 调 MCP 工具 `limem_forget`（参数：`event_id`）
3. 展示回执：
   ```
   ✅ 已归档 <event_id 前 12 位>
      后端：archived
      本地：N 行 tombstone
   ```
   该规则不会再出现在未来任何会话的硬召回 / 软召回 / Pattern 命中中。

## 注意
- 归档是软删除，可后续在 LiMem Web 控制台恢复
- 若用户明确要"硬删除"，目前不支持（设计就是为了防误删）
