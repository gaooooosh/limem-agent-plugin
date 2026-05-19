---
name: limem.pattern
description: >-
  Read, write, or extend an entity's markdown profile in LiMem. Each registered
  entity can hold ONE markdown document; the backend slices it by H2 headings
  for recall, and the client injects matching sections on every UserPromptSubmit.
  Use this skill when the user wants to attach long-form context to an entity
  (usage notes, do/don't, trigger phrasings, references), or asks "show me what
  LiMem knows about <entity>", or to amend an existing profile.
arguments: [entity_id_or_canonical, mode]
---

# /limem.pattern — Entity Markdown 档案的唯一编辑入口

每个注册到 LiMem 的实体可以挂**一篇** markdown 文档（archive 之外的所有信息载体）。
后端按 `^## ` 切片，每段独立打分；客户端在 UserPromptSubmit 时按相关性注入命中段。

`/limem.remember` 只写 event + 注册 entity，**不会**自动生成档案——档案必须通过本 skill
显式编辑。文档结构完全自由，建议（但不强制）用 H2 分节：

- `## 用法` / `## How to use`
- `## 触发` / `## Trigger phrases`（同义说法、口语化、跨语言变体）
- `## 反例` / `## Don't`（明确禁止的用法、对比项）
- `## 相关` / `## See also`（关联命令、对照表）

## 何时调用

- 用户说 "给 X 写个档案" / "把 X 的用法记一下" / "update the profile of X"
- 用户问 "limem 里关于 X 存了什么" / "show me <entity> profile"
- 自动建议：在 `/limem.remember` 完成后、且新建了高 importance entity 时，可以
  主动建议运行本 skill 补充档案

## 参数解析

- `$1` = entity 标识：可以是
  - 完整 `entity_id`（如 `npm_run_dev_command_a1b2c3d4e5`）
  - 实体的 canonical（如 `npm run dev`）—— skill 内部会先调 `limem_list` 或
    `limem_search` 反查 entity_id；找不到则提示用户先 `/limem.remember`
- `$2` = `mode`：`get` / `put` / `append`，缺省 `put`
  - `get` —— 拉取并展示当前 markdown，不写后端
  - `put` —— 整篇覆盖（需要用户确认；新内容为 `$3..` 多行文本，或交互式提示用户粘贴）
  - `append` —— 内部先 `get`，把新增段落拼到末尾再 `put`；适合"加一节反例"

## 处理步骤

### Step 1 — 解析 entity_id

1. 把 `$1` 当 entity_id：调 `limem_pattern_get`，若 `has_pattern=false` 但 entity 自身存在
   则进入下一步；若返回 `404`/`未注册` 错误则进 1.b。
1.b 把 `$1` 当 canonical：调 `limem_search` 用该字符串查；从命中 `patterns[]` 或
   `events[].canonicals[]` 中筛 entity_id。若多个候选，列出供用户二选一。
1.c 若仍找不到 → 输出：
    ```
    ⚠️ 在 LiMem 中找不到与 "<input>" 关联的实体。
       请先运行 /limem.remember 说明这条规则/事实，再回来编辑档案。
    ```
    终止。

### Step 2a — `mode=get`

调 `limem_pattern_get(entity_id)`，渲染：

```
📄 Entity <entity_id> 的当前档案（共 <total_chars> 字符）：

<content（原样 markdown，不要二次格式化）>

如需修改：再次运行 /limem.pattern <entity_id> put
如需追加章节：/limem.pattern <entity_id> append
```

若 `has_pattern=false` → 提示"该实体尚无档案"，给出 put 模板示例。

### Step 2b — `mode=put`

1. 先 `limem_pattern_get` 拉旧版本作为对照（若有）。
2. **强制确认 UI**（覆盖语义不可逆——后端无版本历史）：

   ```
   ⚠️ 即将整篇覆盖 entity <entity_id> 的档案。
      旧版本字符数：<old_total_chars>
      新版本字符数：<new_total_chars>

      旧档案前 5 行：
        ...
      新档案前 5 行：
        ...

   确认覆盖？[y / n]
   ```

3. 用户 `y` → 调 `limem_pattern_put(entity_id, content)`；其它 → "已取消"。
4. 回执：
   ```
   ✓ entity <entity_id> 档案已 <created|updated>（总字符 <total_chars>）。
     下次提到与该实体相关的话题时会自动召回 H2 切片。
   ```

### Step 2c — `mode=append`

1. 拉 `limem_pattern_get`。
2. 把用户提供的新段落（以 `## ` 开头建议）拼到末尾：
   ```
   <旧 content>\n\n## <新标题>\n<新内容>
   ```
3. 走 Step 2b 的确认 UI（同样要求 y）。
4. 调 `limem_pattern_put`。

### Step 3 — 错误处理

- `404 entity_id` 未注册 → 同 Step 1.c
- `422 content blank` → 提示用户至少写一段
- `403` → 该 API key 缺 `w` scope，提示运行 `limem ping`
- 网络错误 → 显示 `LimemError`，建议 retry

## 调用语法（Claude Code / Codex 均可）

- Claude Code：`/limem.pattern <entity_id_or_canonical> [get|put|append]`
- Codex：`$limem.pattern <entity_id_or_canonical> [get|put|append]`

## 备注

- 这是 **整篇 upsert**——后端不支持局部段落 PATCH。要保留旧内容请走 `append`
  或在 `put` 前自己手动拼接。
- 删除整篇档案请用 MCP 工具 `limem_pattern_delete`（无对应 skill，避免误操作）。
- 召回时机：UserPromptSubmit。本地 entity FTS 命中 → 并发拉 markdown 切片 →
  注入到 `<limem_memory>` 的"## 实体档案"段。
