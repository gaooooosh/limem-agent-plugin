---
name: limem.remember
description: >-
  Persist a user-stated rule, preference, feedback, or fact to LiMem long-term
  memory so it is reliably recalled in future sessions. Invoke explicitly when
  the user says "remember X", "from now on don't Y", "always prefer Z", or
  similar; also invoke after the user gives strong feedback ("no, do it like
  this instead") to lock that lesson in. Extracts named entities (canonical +
  role + aliases) so the entity index can match paraphrased prompts; long-form
  documentation (markdown profile) is attached separately via /limem.pattern.
arguments: [text]
---

# /limem.remember — 把用户陈述固化为长期记忆（event + entity 注册）

## 何时调用
- 用户**显式**说 "remember X" / "以后这个项目不要 Y" / "always Z" / "记住 W"
- 用户对刚才输出强烈纠正（"不对，应该…"），此时主动建议保存为 feedback
- 用户在 Claude Code 输入 `/limem.remember <text>` 或 Codex 输入 `$limem.remember <text>` 时

## 边界（v2）

本 skill **只写 event + 注册 entity**：
- event：进入 BM25 软召回池，summary 由后端 LLM 抽取，importance 由 mem_type 决定。
- entity：把规则关心的实体（canonical / aliases / description）注册到后端，并镜像到
  本地 entity_index，让 UserPromptSubmit 的 entity FTS 能命中。

本 skill **不写** entity 的 markdown 档案。要附长文档（用法 / 反例 / 触发短语集合）请用
`/limem.pattern <canonical>`——这是 v2 的设计分工。

## 处理步骤（严格按顺序）

### Step 1 — 解析输入与命名实体抽取

把 `$1 / $ARGUMENTS` 当成"用户原话"。从中抽出**命名实体**（每个实体抽这些字段）：

- `canonical`：实体的规范名（如 `npm run dev`、`docker rebuild`、`react-query`、`/api/v2`）
- `role`：在规则中扮演的角色：
  - `forbidden` — 被禁止 / 不允许的事
  - `preferred` — 被推荐 / 应该用的事
  - `subject` — 规则关心的主题但本身不禁不推（如"前端"、"数据库迁移"）
  - `neutral` — 纯 fact 类引用
- `aliases`（可选）：已知的别名 / 写法变体（命令家族、缩写、中英文版本）。建议 2–6 个；
  这些直接进入本地 entity FTS5 索引，是匹配口语化 prompt 的关键。
- `description`（可选）：一句话描述该实体在本规则下的含义（≤ 60 字符），后端用其驱动
  description_embedding（重要实体的精确链接靠它）。

抽取要点：
1. 单条 `remember` 通常 1–3 个 entity；不要无中生有。
2. canonical 必须是用户原话或一对一可还原的规范化；不要意译。
3. aliases 给跨工具家族（npm/yarn/pnpm/bun）、跨语言（中文/英文）、口语 / 正式形态。
4. **不要**列触发短语爆炸式扩展——长文档走 `/limem.pattern`。

### Few-shot 范例

**例 1**：`"以后这个项目不要用 npm run dev，直接 docker rebuild"`
```json
{
  "text": "以后这个项目不要用 npm run dev，直接 docker rebuild",
  "mem_type": "rule",
  "importance": 0.9,
  "entities": [
    {
      "canonical": "npm run dev",
      "role": "forbidden",
      "aliases": ["npm dev", "yarn dev", "pnpm dev", "bun dev", "起一下 dev"],
      "description": "本项目被禁用的本地开发命令"
    },
    {
      "canonical": "docker rebuild",
      "role": "preferred",
      "aliases": ["docker compose up --build", "重建 docker", "重新构建容器"],
      "description": "替代 npm run dev 的容器化开发流程"
    }
  ]
}
```

**例 2**：`"always prefer pnpm over npm in JS projects"`
```json
{
  "text": "always prefer pnpm over npm in JS projects",
  "mem_type": "preference",
  "importance": 0.85,
  "entities": [
    {"canonical": "pnpm", "role": "preferred",
     "aliases": ["pnpm install", "pnpm add", "pnpm i"]},
    {"canonical": "npm",  "role": "forbidden",
     "aliases": ["npm install", "npm i", "npm ci"]}
  ]
}
```

**例 3**：`"the backend API moved to /api/v2"`
```json
{
  "text": "the backend API moved to /api/v2",
  "mem_type": "fact",
  "importance": 0.7,
  "entities": [
    {"canonical": "/api/v2", "role": "subject",
     "aliases": ["api v2", "api/v2", "backend api"],
     "description": "后端 API 当前生效前缀"}
  ]
}
```

### Step 2 — 显示 scope 二选一交互

输出一段**结构化预览**给用户：

```
📝 准备保存为 LiMem 长期记忆：

  "<text>"

  类型：rule
  识别到的命名实体（共 N 个）：
    • [forbidden] npm run dev   aliases: npm dev / yarn dev / 起一下 dev / ...
    • [preferred] docker rebuild aliases: docker compose up --build / 重建 docker / ...

请选择作用域：
  [1] 项目级（仅当前 git remote=<project_id> 时召回）  ← 推荐
  [2] 全局（任意项目都召回）
  [c] 取消

请回复 1 / 2 / c。
```

**等待用户回复**后再继续 Step 3。

### Step 3 — 调用 MCP 工具 `limem_write`

收到选择后：
- `1` → `scope="project"`
- `2` → `scope="global"`
- `c` 或其它 → "已取消" 并终止

调用 `limem_write` 工具（entities 形如 `{canonical, role, aliases, description, entity_type}`，
**不要传 patterns 字段**——后端 v2 已下线 trigger 数组）。

### Step 4 — 给用户回执 + 提示档案入口

成功后展示：
```
✅ 已保存到 LiMem（id=<event_id 前 12 位>）
   作用域：<scope>
   后端：1 条 event + N 个 entity（<canonical 列表>）
   本地索引：N 个实体已加入 entity FTS（口语化 prompt 也能命中）

💡 若想给这些实体写更长的档案（用法 / 反例 / 参考），运行：
   /limem.pattern <canonical>
```

失败（LimemError / redact）则直接展示错误：
- redact 错误 → 让用户改写避开 `sk-` / `AKIA` / `Bearer ` 等敏感前缀
- 401 → 提示运行 `limem ping` 检查凭证
- 其它 → 把错误原文回显

## 调用语法（Claude Code / Codex 均可）

- Claude Code：`/limem.remember <text>`
- Codex：`$limem.remember <text>` 或在 `/skills` 列表中选择
- 也可作为模型主动决策的工具：在用户给出强偏好后，模型可以问"要把这条记下吗？"然后调本 skill
