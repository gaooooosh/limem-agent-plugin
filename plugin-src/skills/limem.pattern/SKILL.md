---
name: limem.pattern
description: >-
  Read, write, or extend a principal's markdown profile (user / agent / project
  / team / service). Each principal can hold ONE markdown document; the backend
  slices it by H2 headings for recall, and the client injects matching sections
  on every UserPromptSubmit. Use this skill when the user wants to attach
  long-form context (usage notes, do/don't, conventions, references) to a
  principal, or asks "show me what LiMem knows about <user/project/agent>", or
  to amend an existing profile.
arguments: [principal_alias_or_id, mode]
---

# /limem.pattern — Principal Markdown 档案的唯一编辑入口

每个 principal（user / agent / project / team / service）可以挂**一篇** markdown 文档。
后端按 `^## ` 切片，每段独立打分；客户端在 UserPromptSubmit 时按相关性注入命中段。

`/limem.remember` 只写 event，**不会**自动生成档案——档案必须通过本 skill 显式编辑。
文档结构完全自由，建议（但不强制）用 H2 分节：

- `## 命令规约` / `## Commands`（项目偏好/禁用的开发命令）
- `## 用法` / `## How to use`
- `## 反例` / `## Don't`
- `## 风格` / `## Style`（代码风格、注释语言、命名）
- `## 相关` / `## See also`

## 何时调用

- 用户说 "给项目写个档案" / "把约定记下来" / "update my agent profile"
- 用户问 "limem 里关于本项目存了什么" / "show me my user profile"
- 自动建议：在 `/limem.remember` 完成后、scope=project 且 mem_type=rule 累计较多时，
  主动建议把规约 append 到 project principal

## 参数解析

- `$1` = principal 标识：
  - 别名：`project` / `user` / `agent`（最常用；自动解析到当前会话的 stable id）
  - 或完整 `entity_id`（形如 `principal_project_<sha8>` / `principal_user_<sha8>` /
    `principal_agent_<slug>`）
- `$2` = `mode`：`get` / `put` / `append`，缺省 `get`
  - `get` —— 拉取并展示当前 markdown，不写后端
  - `put` —— 整篇覆盖（需要用户确认；新内容从交互式提示读取多行）
  - `append` —— 内部先 `get`，把新增段落拼到末尾再 `put`；适合"加一节规约"

## 处理步骤

### Step 1 — 解析 principal

直接把 `$1` 传给 `limem_pattern_get / put / delete` 即可——这些工具内部已经支持
`project / user / agent` 别名解析。如果输入是用户编造的别名（如某个 canonical 字符串），
工具会在 entity 不存在时返回错误，按 Step 3 处理。

### Step 2a — `mode=get`

调 `limem_pattern_get(entity_id="<alias_or_id>")`，渲染：

```
📄 Principal <entity_id> 的当前档案（共 <total_chars> 字符）：

<content（原样 markdown，不要二次格式化）>

如需修改：再次运行 /limem.pattern <alias> put
如需追加章节：/limem.pattern <alias> append
```

若 `has_pattern=false` → 提示"该 principal 尚无档案"，给出 put 模板示例。

### Step 2b — `mode=put`

1. 先 `limem_pattern_get` 拉旧版本作为对照（若有）。
2. **强制确认 UI**（覆盖语义不可逆——后端无版本历史）：

   ```
   ⚠️ 即将整篇覆盖 principal <entity_id> 的档案。
      旧版本字符数：<old_total_chars>
      新版本字符数：<new_total_chars>

      旧档案前 5 行 / 新档案前 5 行 对照...

   确认覆盖？[y / n]
   ```

3. 用户 `y` → 调 `limem_pattern_put(entity_id, content)`；其它 → "已取消"。
4. 回执：
   ```
   ✓ principal <entity_id> 档案已 <created|updated>（总字符 <total_chars>）。
     下次 SessionStart 与匹配 prompt 时会自动召回 H2 切片。
   ```

### Step 2c — `mode=append`

1. 拉 `limem_pattern_get`。
2. 把用户提供的新段落（建议以 `## ` 开头）拼到末尾：
   ```
   <旧 content>\n\n## <新标题>\n<新内容>
   ```
3. 走 Step 2b 的确认 UI。
4. 调 `limem_pattern_put`。

### Step 3 — 错误处理

- `404` principal 不存在 → 提示用户先用 `/limem.entity register` 注册，或检查 alias
- `422 content blank` → 提示用户至少写一段
- `403` → API key 缺写权限，提示运行 `limem ping`
- 网络错误 → 显示 `LimemError`，建议 retry

## 调用语法

- Claude Code：`/limem.pattern <project|user|agent|entity_id> [get|put|append]`
- Codex：`$limem.pattern <project|user|agent|entity_id> [get|put|append]`

## 备注

- 这是**整篇 upsert**——后端不支持局部段落 PATCH。要保留旧内容请走 `append`
  或在 `put` 前自己手动拼接。
- 删除整篇档案请用 MCP 工具 `limem_pattern_delete`（无对应 skill，避免误操作）。
- 召回时机：UserPromptSubmit + SessionStart。active principals 并发拉切片，
  注入到 `<limem_memory>` 的 "## 实体档案" 段。
