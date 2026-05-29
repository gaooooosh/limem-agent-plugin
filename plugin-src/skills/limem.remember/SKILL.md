---
name: limem.remember
description: >-
  Persist a user-stated rule, preference, feedback, or fact to LiMem long-term
  memory so it is reliably recalled in future sessions. Invoke explicitly when
  the user says "remember X", "from now on don't Y", "always prefer Z", or
  similar; also invoke after the user gives strong feedback ("no, do it like
  this instead") to lock that lesson in. v3: `entities` describe mentions
  (canonical + aliases) for BM25 indexing — they are NOT registered as backend
  entities. To attach a markdown profile to user / agent / project, use
  /limem.pattern.
arguments: [text]
---

# /limem.remember — 把用户陈述固化为长期记忆（event 写入 + mention 抽取）

## 何时调用
- 用户**显式**说 "remember X" / "以后这个项目不要 Y" / "always Z" / "记住 W"
- 用户对刚才输出强烈纠正（"不对，应该…"），此时主动建议保存为 feedback
- 用户在 Claude Code 输入 `/limem.remember <text>` 或 Codex 输入 `$limem.remember <text>` 时

## 边界（v3）

本 skill **只写 event**：
- event：进入 BM25 软召回池，summary 由后端 LLM 抽取，importance 由 mem_type 决定。
- mentions（旧字段名 `entities`）：抽取的 canonical / aliases 进入 BM25 tag-token 与本地
  `raw_metadata.canonicals`，召回时增强匹配；**不再注册为后端 entity**。
- `principal_ids`：根据 scope / mem_type 自动推断本次 event 应挂的 principals
  （user / agent / project），写入本地 metadata，soft 召回时降权过滤。

本 skill **不写** principal 的 markdown 档案。要附长文档（用法 / 反例 / 约定）请用
`/limem.pattern project|user|agent`——这是 v3 的设计分工。

LiMem 现在按上下文相关性召回，canonical / aliases 会作为 trigger 辅助命中；它不再适合保存"必须每轮生效"的无条件约束。始终生效的要求应写入 `CLAUDE.md`、`AGENTS.md` 或全局系统指令。

## 处理步骤（严格按顺序）

### Step 1 — 解析输入与 mention 抽取

把 `$1 / $ARGUMENTS` 当成"用户原话"。从中抽出**关键 mentions**（每个抽这些字段）：

- `canonical`：mention 的规范名（如 `npm run dev`、`docker rebuild`、`react-query`、`/api/v2`）
- `role`：在规则中扮演的角色：
  - `forbidden` — 被禁止 / 不允许的事
  - `preferred` — 被推荐 / 应该用的事
  - `subject` — 规则关心的主题但本身不禁不推（如"前端"、"数据库迁移"）
  - `neutral` — 纯 fact 类引用
- `aliases`（可选，建议 2–6 个）：跨工具家族、跨语言、口语 / 正式形态的同义说法。
  这些进入 BM25 tag-token 与本地 metadata 镜像，是匹配口语化 prompt 的关键。
- `description`（可选）：一句话描述该 mention 在本规则下的含义（≤ 60 字符），仅落本地
  metadata 不上行后端。

抽取要点：

1. 单条 `remember` 通常 1–3 个 mention；不要无中生有。
2. canonical 必须是用户原话或一对一可还原的规范化；不要意译。
3. aliases 给跨工具家族（npm/yarn/pnpm/bun）、跨语言（中文/英文）、口语 / 正式形态。
4. **不要**枚举大量触发短语——长文档与"档案级"约定走 `/limem.pattern`。

### Few-shot 范例

**例 1**：`"以后这个项目不要用 npm run dev，直接 docker rebuild"`
```json
{
  "text": "以后这个项目不要用 npm run dev，直接 docker rebuild",
  "scope": "project",
  "mem_type": "rule",
  "importance": 0.9,
  "entities": [
    {
      "canonical": "npm run dev",
      "role": "forbidden",
      "aliases": ["npm dev", "yarn dev", "pnpm dev", "bun dev", "起一下 dev"]
    },
    {
      "canonical": "docker rebuild",
      "role": "preferred",
      "aliases": ["docker compose up --build", "重建 docker", "重新构建容器"]
    }
  ]
}
```

**例 2**：`"always prefer pnpm over npm in JS projects"`
```json
{
  "text": "always prefer pnpm over npm in JS projects",
  "scope": "global",
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

### Step 2 — 显示 scope 二选一交互

输出结构化预览（含 mention 列表 + 类型 + 推断的 principals 说明）后等待用户回复
`1 / 2 / c`。

### Step 3 — 调用 MCP 工具 `limem_write`

收到选择后：
- `1` → `scope="project"`
- `2` → `scope="global"`
- `c` 或其它 → "已取消" 并终止

调用 `limem_write`（参数中的 `entities` 入参是 mention，**不再注册后端 entity**）。

### Step 4 — 给用户回执 + 提示档案入口

成功后展示：
```
✅ 已保存到 LiMem（id=<event_id 前 12 位>）
   作用域：<scope>  类型：<mem_type>
   关联 principals：<principal_ids 列表>
   关联 mentions：<canonicals 列表>

💡 若想把这条约定固化为长期档案（用法 / 反例 / 命令规约），运行：
   /limem.pattern project append    # 项目级约定
   /limem.pattern user append       # 跨项目偏好
   /limem.pattern agent append      # 对 AI 行为的约束
```

失败处理：
- redact 错误 → 让用户改写避开 `sk-` / `AKIA` / `Bearer ` 等敏感前缀
- 401 → 提示运行 `limem ping` 检查凭证
- 其它 → 把错误原文回显

## 调用语法

- Claude Code：`/limem.remember <text>`
- Codex：`$limem.remember <text>`
- 也可作为模型主动决策的工具：在用户给出强偏好后，模型可以问"要把这条记下吗？"然后调本 skill
