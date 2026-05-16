---
name: limem.remember
description: >-
  Persist a user-stated rule, preference, feedback, or fact to LiMem long-term
  memory so it is reliably recalled in future sessions. Invoke explicitly when
  the user says "remember X", "from now on don't Y", "always prefer Z", or
  similar; also invoke after the user gives strong feedback ("no, do it like
  this instead") to lock that lesson in. Performs explicit entity & trigger-
  pattern extraction so even paraphrased recall hits (e.g. "起一下 dev" matches
  a rule about "npm run dev").
arguments: [text]
---

# /limem.remember — 把用户陈述固化为长期记忆

## 何时调用
- 用户**显式**说 "remember X" / "以后这个项目不要 Y" / "always Z" / "记住 W"
- 用户对刚才输出强烈纠正（"不对，应该…"），此时主动建议保存为 feedback
- 用户在 Claude Code 输入 `/limem.remember <text>` 或 Codex 输入 `$limem.remember <text>` 时

## 处理步骤（严格按顺序）

### Step 1 — 解析输入与命名实体抽取

把 $1 / $ARGUMENTS 当成"用户原话"。从中抽出**命名实体**与每个实体的**触发短语集合**：

- canonical：实体的规范名（如 `npm run dev`、`docker rebuild`、`react-query`、`/api/v2`）
- role：在规则中扮演的角色：
  - `forbidden` — 被禁止 / 不允许的事
  - `preferred` — 被推荐 / 应该用的事
  - `subject` — 规则关心的主题但本身不禁不推（如"前端"、"数据库迁移"）
  - `neutral` — 纯 fact 类引用
- patterns：尽量多的同义触发短语；用户后续以**任意一种**说法提到这个实体时都应该命中。

抽取要点（务必遵守）：
1. 中英对照：每个 canonical 至少给出中文 + 英文/技术词形（"启动开发服务器" + "dev server" + "run dev"）
2. 同义动词组合：跑/起/起一下/启动/本地起 + dev → 多个 patterns
3. 工具家族变体：npm / yarn / pnpm / bun + dev
4. 用户口语化表达：让前端跑起来 / 跑一下 / 起一下
5. 至少 5 个 patterns / 实体；越多越好（不超过 30）
6. 单个 pattern 最少 2 字符；不要纯单字（"a"、"x"）

### Few-shot 范例（必读）

**例 1**：输入 `"以后这个项目不要用 npm run dev，直接 docker rebuild"`
```json
{
  "text": "以后这个项目不要用 npm run dev，直接 docker rebuild",
  "mem_type": "rule",
  "entities": [
    {"canonical": "npm run dev", "role": "forbidden",
     "patterns": ["npm run dev","npm dev","yarn dev","pnpm dev","bun dev",
                  "run dev","start dev","起 dev","起一下 dev",
                  "启动开发服务器","本地开发服务器","dev server",
                  "跑一下 dev","让前端跑起来","本地起一下"]},
    {"canonical": "docker rebuild", "role": "preferred",
     "patterns": ["docker rebuild","docker compose up --build",
                  "docker-compose up --build","重建 docker","rebuild docker",
                  "重新 build 镜像","rebuild container","重新构建容器"]}
  ]
}
```

**例 2**：输入 `"always prefer pnpm over npm in JS projects"`
```json
{
  "text": "always prefer pnpm over npm in JS projects",
  "mem_type": "preference",
  "entities": [
    {"canonical": "pnpm", "role": "preferred",
     "patterns": ["pnpm","pnpm install","pnpm add","pnpm i"]},
    {"canonical": "npm", "role": "forbidden",
     "patterns": ["npm","npm install","npm i","npm add","npm ci"]}
  ]
}
```

**例 3**：输入 `"the backend API moved to /api/v2"`（fact 类，无 forbidden/preferred）
```json
{
  "text": "the backend API moved to /api/v2",
  "mem_type": "fact",
  "entities": [
    {"canonical": "/api/v2", "role": "subject",
     "patterns": ["/api/v2","api v2","api/v2","backend api","API 前缀"]}
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
    • [forbidden] npm run dev (15 个触发短语)
    • [preferred] docker rebuild (8 个触发短语)

请选择作用域：
  [1] 项目级（仅当前 git remote=<project_id> 时召回）  ← 推荐
  [2] 全局（任意项目都召回）
  [c] 取消

请回复 1 / 2 / c。
```

`<project_id>` 来自调用 MCP `limem_ping` 或本地 detect，必须显式列出，让用户清楚规则只在该项目生效。

**等待用户回复**后再继续 Step 3。**不要假定用户输入"1"就跳过这步**——必须明确给出确认 UI。

### Step 3 — 调用 MCP 工具 `limem_write`

收到用户选择后：
- `1` → scope="project"
- `2` → scope="global"
- `c` 或其它 → 输出 "已取消" 并终止

调用 `limem_write` 工具（参数见 Step 1 抽取结果，加上 scope）。

### Step 4 — 给用户回执

成功后展示：
```
✅ 已保存到 LiMem（id=<event_id 前 12 位>）
   作用域：<scope>
   后端：1 条 event + 2 个 entity（npm run dev, docker rebuild）
   本地索引：18 个触发短语已加入 Pattern Index
   未来你在本项目任何会话提到这些短语都会自动召回该规则。
```

失败（LimemError / redact）则直接展示错误，并提示用户：
- redact 错误 → 让用户改写避开 `sk-` `AKIA` 等敏感前缀
- 401 → 提示运行 `limem ping` 检查凭证
- 其它 → 把错误原文回显

## 调用语法（Claude Code / Codex 均可）

- Claude Code: `/limem.remember <text>`
- Codex: `$limem.remember <text>` 或在 `/skills` 列表中选择
- 也可作为模型主动决策的工具：在用户给出强偏好后，模型可以问"要把这条记下吗？"然后调本 skill
