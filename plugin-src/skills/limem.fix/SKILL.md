---
name: limem.fix
description: >-
  Update an existing memory **event's** text in-place via short_id (#xxxx). Use
  when user wants to refine an event's wording without creating a new record
  ("把那条改成 …", "fix the rule about npm dev"). Calls MCP `limem_fix`. Does
  NOT touch entity markdown profiles — use /limem.pattern for those.
arguments: [short_id, new_text]
---

# /limem.fix — 修订已有 event 文本（通过 short_id）

## 边界（v2）

本 skill 只改 **event** 的原文 / summary（后端 `graph/update` + 本地 event_metadata 镜像）。
它**不会**修改任何 entity 的 markdown 档案——后端 pattern 是另一份独立资源，
若想修订档案请使用 `/limem.pattern <entity_id_or_canonical>`。两条路径正交，
保持职责清晰，避免一次 fix 同时改两处导致历史链混乱。

## 何时调用
- 用户在召回区块看到 `#abc123def456` 后说"把那条改成 …"
- 用户希望保留 event_id 历史链但更新文本
- 用户错字 / 表达不准要更精确写法

## 处理步骤

1. 解析 `$1` = short_id（接受 `#xxxx` 或裸 hex），`$2..` = 新文本
2. 调 MCP `limem_fix`：
   ```json
   {"short_id": "<去掉 # 的 hex>", "new_text": "<新文本>"}
   ```
3. 回执：
   ```
   ✓ 已更新 LiMem event #abc123def456：
       <旧文本>
     → <新文本>

   提示：如果你是想修订某个实体的档案而不是 event 文本，使用：
       /limem.pattern <entity_id_or_canonical>
   ```

## 注意

- 这是 **update**，不是 create；event_id 不变，历史链保留
- 若 short_id 不在本地映射，工具会返回 NOT_FOUND_SHORT_ID 错误，提示用户检查 id 拼写
- 不会重新跑 entity 抽取（即新文本里出现新实体不会自动注册）
