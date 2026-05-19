# PRD：LiMem 无感长期记忆增强（外置式）

> **文档类型**：需求文档风格 prompt，目标读者 = 实施 agent / 工程师
> **版本**：v0.1（2026-05-16 起草）
> **作者视角**：产品经理
> **目标读者**：负责实施 limem-cli 的工程 agent

---

## 0. TL;DR（给实施 agent 的一句话）

把所有"分析/提议/状态展示"逻辑从 hook 同步路径剥离到一个新增的后台 daemon（`limemd`）中，主对话上下文里**只允许**保留两类内容：召回结果、致命错误降级单行 banner。其余信息走 statusline + TUI + 本地通知三条**外置通道**交付。

---

## 1. 背景

当前 LiMem 已实现：UserPromptSubmit / SessionStart 召回注入、`/limem.remember` 显式写入、SQLite 本地镜像、Codex idle flush 等核心管道。但用户旅程仍有显式动作（手动 `limem init --project`、靠用户主动说"记住"才学习、召回失败无可见反馈、AGENTS.md 被注入占位块），且后续要做的"被动学习提议"若沿用"往主对话注 banner"的做法将污染 agent 上下文。

## 2. 目标 & 非目标

**目标**
1. **无感接入**：用户在 Claude Code / Codex 中不需要任何"我在用 LiMem"的认知动作
2. **不污染主 agent 上下文**：提议、统计、调试、待办**绝不**进 `additionalContext`
3. **失败可见**：静默失败必须能被用户看到，但不打扰主对话

**非目标（本期明确不做）**
- 跨设备 OAuth / device-code 登录（属 P0-1，下期）
- limem dash 的 Web UI（本期只做 TUI）
- 隐式学习对接后端 LLM 抽取（先用正则 + 相似度降级方案）
- 跨 commit 的语义模式挖掘（F3 高级特性留下期）

## 3. 设计原则（必须遵守）

| # | 原则 | 含义 |
|---|---|---|
| P1 | **External-First** | 任何超过 50ms 或需要 LLM/分类的逻辑禁止跑在 hook 同步路径；放进 `limemd` 后台异步消费 |
| P2 | **单向数据流** | hook 只**写**事件到本地事件总线，不直接做重活；daemon 异步消费；statusline/TUI 从 daemon read API 拉 |
| P3 | **上下文预算守恒** | 注入区块字节数上限不增（仍受 `inject_budget_*` 约束）；新增的"理由/短 id"字段从 per-item 预算里挤 |
| P4 | **静默失败=禁止** | 后端调用失败必须通过 statusline 状态变色 + 注入区改为单行 status 行让用户看见 |
| P5 | **后端契约耦合仅一处** | 所有改动不得绕过 `limem/client.py` 拼 HTTP；新功能通过组合现有客户端方法实现 |

## 4. 体系结构

```
┌─────────────────┐    NDJSON / socket   ┌─────────┐    read API    ┌──────────────┐
│ Claude / Codex  │ ── hooks ──────────▶│ limemd  │ ◀──────────────│ statusline / │
│   (主对话)      │ ◀── 召回 ────────────│(daemon) │                │  TUI dash    │
└─────────────────┘  additionalContext   └────┬────┘                └──────────────┘
                                              │
                                              ▼ LimemClient → LiMem 后端
```

**新增组件**
- `limemd` — 长生命周期 daemon，单实例锁文件 `~/.cache/limem/limemd.pid`，IPC 走 unix socket `~/.cache/limem/limemd.sock`
- `limem dash` — TUI 面板（rich.live 或 textual）
- `limem statusline` — 短命令，stdout 输出当前 statusline 串

**事件总线**：`~/.cache/limem/events.ndjson`（hooks.log 升级形态：从纯调试日志变成 daemon 的输入队列）

## 5. 功能需求

> 语义：**必须 (MUST)** / **应当 (SHOULD)** / **可以 (MAY)**

### F1（P0-3）项目级 init 自动化

**触发**：UserPromptSubmit hook 检测 cwd 在 git 仓库内 且 `.limem/local.json` 不存在 → 静默写文件 + append `.gitignore`

**约束（MUST）**
- 不修改 CLAUDE.md / AGENTS.md（与 F10 一致）
- 不向 additionalContext 写任何 banner——首次初始化提示走系统通知（best-effort：`notify-send` / `osascript`）+ statusline 临时标志位 `inited_now=true`，仅 5 分钟

**验收**
1. clone 一个新 git 仓库 → 第一次 prompt → `.limem/local.json` 存在
2. 主对话上下文中无任何 LiMem 文本
3. 仓库 `.gitignore` 末尾追加了 `.limem/local.json`

---

### F2（P1-4）重复纠正 → 提议保存（**无每日上限**）

**采集**：UserPromptSubmit hook 把 `{prompt, ts, project_id, scope, session_id}` append 到 events.ndjson

**分析（daemon 周期 60s）**
- 关键词正则识别纠正句式：中文 `不对|应该|别|不要|改成|纠正|换成|不是` / 英文 `(?i)don'?t|actually|stop|wrong|instead|prefer`
- 命中后取过去 24h 同 project_id 内的事件，用 **trigram Jaccard 相似度 ≥ 0.4** 聚合同主题
- 聚合 ≥ 2 条 → 形成候选；触发 `LimemClient` 调后端抽取 entity（本期降级：本地正则抽取 canonical + 直接落 suggestions）

**交付（MUST 全部外置）**
- 候选写 `~/.cache/limem/suggestions.json`：数组项 `{id, scope, candidate_text, mem_type, evidence_event_ids, confidence, created_ts}`
- statusline 加 `💡 N` 徽章
- 用户跑 `limem dash` 见详情；按键操作：`a` 接受（→ 调 memory_writer.remember）/ `d` 丢弃 / `e` 编辑后接受

**严禁（MUST NOT）**
- 不向 additionalContext 注入"我注意到 …"
- 不调主 agent 让它判断（保持外置）
- 不设每日上限——dash 默认按 confidence 倒序展示足以避免烦扰

**验收**
1. 用户在三次连续 prompt 中纠正同一主题 → 60s 内 suggestions.json 出现一条候选
2. 主对话上下文中无任何提议文本
3. 跑 `limem dash` 在候选列表中按 `a` → 真实写入后端 → 下次 SessionStart 该规则进入硬召回

---

### F3（P1-5）接受的代码改动 → preference 候选

**采集**：新增 PostToolUse hook（Claude Code 已支持，Codex 若无则降级为只采 UserPromptSubmit 中的"上次回复被接受"信号）

字段：`{tool: "Edit"|"Write"|"NotebookEdit", file_path, diff_summary(<=400 chars), accepted: bool, ts, project_id}`

**分析（daemon）**
- 滑动窗口 7 天
- 对接受的 edit 做简单 N-gram（n=3）频次统计
- 阈值：同一 N-gram 出现 ≥ 5 次 且 接受率 ≥ 80% → 形成 preference 候选，文案模板 `"prefer pattern: {ngram}"`

**降级（SHOULD）**：本期可只交付采集 + N-gram 统计；语义级模式（"用 dataclass 替代 dict"这类需要 AST 的）留下期。N-gram 候选必须经过 `limem dash` 编辑才落库。

**验收**
1. PostToolUse 事件被 daemon 消费，可在 `limem dash --logs` 看到
2. 当 N-gram 阈值满足时，suggestions.json 出现候选
3. 主对话无任何相关注入

---

### F4（P2-6）statusline 状态条

**实现**：新增 `limem statusline` 子命令，stdout 单行格式：
```
📚 7 · ▶ 3 · 💡 2 · ⏸ off
```
字段含义：
- `📚 N` — 当前 project scope 下激活记忆数
- `▶ N` — 本会话累计召回 hit 次数（daemon 维护，SessionStart 清零）
- `💡 N` — 待审候选数
- `⏸ on/off` — pause 状态；on 时显示剩余分钟

**异常态**
- 凭证失效：整行替换 `⚠ LiMem degraded (auth_expired) · run \`limem ping\``
- daemon 未运行：`📴 LiMem daemon off`

**集成（installer 改造）**
- Claude Code：写入 `~/.claude/settings.json` 的 `statusLine` 段（**仅当用户没有同名配置**，否则只输出 note 提示）
- Codex：若 `~/.codex/config.toml` 支持 statusLine 则写入；否则跳过并 note

**性能（MUST）**
- `limem statusline` 必须 < 50ms
- 实现路径：先尝试连 daemon socket 取缓存值；socket 不通则 fallback 读 `~/.cache/limem/statusline.cache.json`（daemon 每 5s 刷一次），保证 daemon 重启时不阻塞 statusline 渲染

**验收**
1. 安装后 Claude Code 状态行出现 LiMem 字段
2. 命中召回时 `▶` 计数随会话上涨
3. pause 后状态行立即反映

---

### F5（P2-7）内联编辑

**注入侧改造（`injector.render_inject`）**
- 每条记忆末尾追加 12 位短 id：`#a1b2c3d4e5f6`（取 event_id 的 sha1[:12]，与 event_id 双向映射存本地 SQLite 新表 `short_id_map`）
- 启动时检测冲突；冲突自动扩到 14 位

**新增 skill / MCP 工具**
- `/limem.fix <short_id> <new_text>` → 调 `client.graph_update_event` 替换 fields.original_text + summary，并同步本地 event_metadata 镜像
- `/limem.no <short_id>` → 写 `~/.cache/limem/session_mute.json`（结构 `{session_id: [short_id...]}`），仅作用于本 session_id

**hook 行为**
- UserPromptSubmit 渲染前过滤 session_mute
- SessionEnd / Stop flush 清理本 session 的 mute

**约束（MUST NOT）**
- fix 操作禁止产生新 event_id（避免历史链断裂）；只能 update
- mute 不写后端

**验收**
1. 召回区块每条带 `#xxxx`
2. `/limem.fix #xxxx 新文本` 后后端 event 文本被替换且本地镜像同步
3. `/limem.no #xxxx` 后本会话剩余轮次不再注入该条；下一会话恢复

---

### F6（P2-8）召回理由可见

**注入区块头改造**：
```
<limem_memory recall="3" via="pattern:起 dev | bm25:docker compose" budget="412/2000" project="github.com/foo/bar">
```
- `via=` 列出本轮命中的关键 pattern 内容（top-3） + bm25 query 关键词（top-2）
- 每条记忆末尾 tag 改为 `src=pattern|bm25|hard`

**约束（MUST）**
- 总注入字符数不增——`per_item_chars` 同步缩 20 字给 header
- 不增加额外条目

**验收**
1. 召回任意非空时 header 含 `via=` 字段
2. via 关键词正确（实测：触发"docker rebuild"召回时 via 里有 `docker` 子串）
3. 注入总字节 ≤ `inject_budget_soft + inject_budget_hard`

---

### F7（P3-9）/limem.pause

**新增 skill / MCP 工具**：`/limem.pause [duration]`，duration 默认 `60m`，支持 `30m` / `2h` / `until-session-end`

**实现**：写 `~/.cache/limem/pause.json`：`{until_ts: int|null, scope: "project"|"global", session_id?: str}`

**hook 行为**
- pause 生效期：UserPromptSubmit 跳过召回，**不向 additionalContext 写任何内容**（连 `<limem_memory>` 空标签都不写）
- SessionEnd / Stop / PreCompact / 被动学习采集**全部停止**

**配套**
- `/limem.resume` — 提前结束 pause
- statusline 显示剩余时间

**验收**
1. `/limem.pause 30m` 后 prompt 无召回注入
2. 30m 后自动恢复
3. pause 期间事件不写入 events.ndjson（被动学习也停）

---

### F8（P3-10）/limem.export

**新增 CLI**：`limem export [--format json|markdown] [--output PATH] [--include-tombstoned]`

**内容**：当前 db 全部 events，每条含 `event_id` / `scope` / `mem_type` / `text` / `original_text` / `ts` / `entity_ids` / `triggers` / `tombstone`

**约束（MUST）**
- 不调召回 / query 端点（避免污染缓存与 BM25 stats）
- 直接拉本地 event_metadata + 对每条调 `client.list_entity_patterns` 补 triggers
- 默认输出 `./limem-export-<ts>.json`，chmod 600

**import 不在本期范围**（占位但不实现）

**验收**
1. `limem export` 产物为合法 JSON
2. 包含本地所有 active events
3. 跑两次产物 diff 仅时间戳字段变化

---

### F9（P3-11）错误降级注入

**daemon 责任**：维护"后端连通性状态"，连续 3 次 401/403/5xx → 切到 `degraded` 状态；恢复 1 次成功 → 切回 `healthy`

**hook 行为**
- `degraded` 期 UserPromptSubmit 改写注入为：
  ```
  <limem_memory status="degraded" reason="auth_expired|server_error|network">
  ⚠️ LiMem 暂不可用（{reason}）。本轮无召回。诊断：`limem ping`
  </limem_memory>
  ```
- **同一 session 内最多注入 1 次**该 banner（已读不烦），其余靠 statusline `⚠ degraded` 持续可见

**严禁（MUST NOT）**
- 写更长的错误堆栈到注入
- 把 errno / 内部异常文本暴露到主对话

**验收**
1. 伪造 401 三次后下一次 prompt 见 degraded banner
2. 同 session 第 2、3 次 prompt 不再注入 banner（仅 statusline 变色）
3. 后端恢复后下一次 SessionStart statusline 回到正常

---

### F10（P4-13）停止污染 CLAUDE.md / AGENTS.md

**改造**：
- `installer.project_init` 删除以下逻辑：
  - 写 AGENTS.md `<!-- limem:rules begin/end -->` 占位块
  - 给 CLAUDE.md 追加 "See AGENTS.md ..." 引用行
- 保留：`.gitignore` 与 `.limem/local.json` 写入

**新增 CLI**
- `limem sync-static [--target AGENTS.md] [--scope project|global]` — 用户主动调用时才生成静态镜像，含 begin/end 标记（用户可自由编辑标记外内容）
- `limem migrate clean-static [--root PATH]` — 一次性帮历史项目移除占位块（按 begin/end 标记定位）

**验收**
1. `limem init --project` 在干净仓库不再修改 CLAUDE.md / AGENTS.md
2. 已存在占位块的项目跑 migrate 后两文件干净（标记外内容保留）
3. `limem sync-static` 显式调用时仍可生成镜像

---

## 6. 非功能需求

| 项 | 要求 |
|---|---|
| Hook 延迟 | stdin → stdout 总耗时 ≤ 200ms（与现 `hook_timeout_ms` 对齐） |
| Daemon 内存 | 常驻 < 80 MB |
| Daemon 崩溃恢复 | 下次 hook 触发自动 fork 拉起；拉起期间 hook 不阻塞，返回空注入 |
| 隐私 | events.ndjson 中 prompt 字段先过 `redact.contains_secret`；总文件保留 7 天后滚动删除 |
| 可观测 | `limem dash --logs` 能看到所有 hook / daemon 决策；`limem dash --suggestions` 看候选 |
| 主对话上下文占用 | 召回注入字节数上限不增；新增 via/short_id 从 per-item 预算挤 |

## 7. 风险与权衡

| 风险 | 缓解 |
|---|---|
| daemon 与 hook 双进程竞争 SQLite | 全部走 daemon socket，hook 不直接读 patterns.sqlite；只有 daemon 持有 sqlite 连接 |
| 候选过多但无每日上限可能堆积 | dash 默认 confidence 倒序，超过 30 天未操作的自动归档；statusline 数字徽章不主动弹通知 |
| short_id 冲突 | 启动时建立映射表，检测到冲突自动扩位；冲突率超过 1% 全局升到 14 位 |
| 历史用户的 AGENTS.md 占位块 | `limem migrate clean-static` 一次性清理；installer 在升级时主动提示运行 |
| daemon 不被任何用户主动启动 | UserPromptSubmit hook 检测到 socket 无响应时静默 fork 拉起（exec `limemd --detach`） |
| Codex 无 PostToolUse | F3 在 Codex 上降级为只采 UserPromptSubmit 内的"上轮 patch 接受"语义 |

## 8. 交付清单（按依赖顺序）

> 实施 agent 可按此顺序拆分子 PR，每个子 PR 自带单元测试 ≥ 5 例并更新 CLAUDE.md "架构要点" 小节。

1. **limemd 骨架**
   - unix socket server + 单实例锁 + 自动拉起逻辑
   - 事件总线消费器（NDJSON tail）
   - read API：`get_status` / `list_suggestions` / `get_recall_stats` / `get_connectivity`
2. **statusline 与 installer 改造**（F4 + F10）
   - `limem statusline` 子命令
   - installer 写 settings.json statusLine 段（不覆盖既有）
   - 移除 AGENTS.md / CLAUDE.md 占位块逻辑
   - `limem migrate clean-static`
3. **降级与暂停**（F7 + F9）
   - `/limem.pause` & `/limem.resume`
   - daemon connectivity state machine
   - hook 注入侧 degraded banner（每 session 一次）
4. **召回链增强**（F5 + F6）
   - short_id_map 表
   - injector 加 `via=` header
   - `/limem.fix` 与 `/limem.no` skill + MCP 工具
5. **导出**（F8）
   - `limem export` CLI
6. **被动学习**（F2 + F3）
   - events.ndjson 采集格式升级
   - PostToolUse hook（Claude Code）
   - daemon Jaccard 聚合器 + N-gram 统计器
   - suggestions.json schema
7. **TUI dash**
   - rich/textual 实现
   - 候选 accept/discard/edit 按键
   - logs viewer
8. **F1 自动 project init**（放最后，等 daemon 与 statusline 都就位后做静默初始化更稳）

## 9. 验收总闸（QA 视角）

完整跑完一遍以下 happy path，全部通过即视为本期完成：

1. 全新机器 `pipx install ./limem-cli` + `limem bootstrap`（沿用现状）
2. `cd ~/some-new-repo` 后第一次和 Claude Code 对话 → `.limem/local.json` 自动生成、AGENTS.md 未被修改、statusline 显示 `📚 0`
3. 连续 3 轮纠正同一主题 → 60s 内 statusline `💡 1`，`limem dash` 见候选，按 `a` 接受
4. 下一轮 prompt 触发该规则召回 → 注入区块带 `via=` 与短 id；输入 `/limem.fix #xxxx 新文本` 后下一轮注入文本已更新
5. `/limem.pause 10m` 后注入完全消失；10m 后自动恢复
6. 拔网 / 改坏 api_key → 第一次 prompt 见 degraded banner，第 2 次起仅 statusline 红字
7. `limem export` 产物含全部记忆且 JSON 合法
8. 全程主对话**未出现**："我注意到..."、"建议保存..."、"候选规则..."、"安装成功..."、错误堆栈等非召回类文本

---

## 附录 A：事件总线 schema（events.ndjson）

每行一个 JSON 对象，字段：

```json
{
  "ts": 1747400000,
  "kind": "user_prompt_submit" | "post_tool_use" | "session_start" | "session_end" | "stop" | "pre_compact" | "recall_emitted" | "backend_call",
  "tool": "claude-code" | "codex",
  "session_id": "sess-abc",
  "project_id": "github.com/foo/bar",
  "scope": "project:github.com/foo/bar",
  "payload": { ... },     // kind-specific
  "redacted": true        // 是否经过 redact
}
```

**`recall_emitted` payload**（由 daemon `_h_report_recall` emit；daemon `_handle_event_row` 入口忽略此 kind 防回放）：

```json
{
  "items": [
    {"short_id": "a3f1c0a3f1c0", "event_id": "evt_...", "src": "hard",
     "mem_type": "rule", "scope": "project:...", "summary_head": "≤60 chars"},
    {"short_id": "", "event_id": "", "src": "pattern",
     "canonical": "project:foo-bar", "heading": "命令规约", "summary_head": "..."}
  ],
  "via_patterns": ["project:foo", "user:u_42"],
  "via_keywords": ["docker", "rebuild"],
  "prompt_head": "起一下 dev",
  "injected_chars": 1832,
  "counts": {"hard": 1, "pattern": 1, "bm25": 1}
}
```

## 附录 B：suggestions.json schema

```json
[
  {
    "id": "sug_2025_0001",
    "kind": "rule" | "preference" | "feedback",
    "scope": "project:github.com/foo/bar",
    "candidate_text": "禁用 npm run dev，改用 docker rebuild",
    "evidence_event_ids": ["evt_1", "evt_2", "evt_3"],
    "confidence": 0.78,
    "extracted_entities": [
      {"canonical": "npm run dev", "role": "forbidden", "patterns": ["npm dev","起 dev"]}
    ],
    "created_ts": 1747400000,
    "status": "pending" | "accepted" | "discarded"
  }
]
```

## 附录 C：daemon read API（unix socket，JSON-RPC over line-delimited JSON）

| method | params | result |
|---|---|---|
| `get_status` | `{}` | `{active_memories, hit_count, suggestion_count, pause, connectivity, last_recall?}` |
| `list_suggestions` | `{status?: "pending"\|"all"}` | `[Suggestion, ...]` |
| `accept_suggestion` | `{id, edited_text?, edited_entities?}` | `{event_id}` |
| `discard_suggestion` | `{id}` | `{ok: true}` |
| `get_recall_stats` | `{since_ts?: int}` | `{hits_by_kind, top_triggers}` |
| `bump_hit` | `{session_id}` | `{ok}` |
| `set_connectivity` | `{state, reason?}` | `{ok}` |
| `report_recall` | `{ts, session_id, project_id, scope, items[], via_patterns[], via_keywords[], prompt_head, injected_chars}` | `{ok: true}` |
| `list_recent_recalls` | `{limit?: int = 20}` | `[RecallEmittedRecord, ...]` newest-first |
| `consume_pending_recall` | `{session_id, dedupe?: bool = true}` | `RecallEmittedRecord \| null`（取后即清；签名与上次相同时返回 null）|

调用方：hook、statusline、dash、MCP server 全部走这一套，禁止任何模块直接读 sqlite 以外的 daemon 状态。

**`report_recall`** 是 hook 在 `_hook_user_prompt_submit` 中 fire-and-forget 调用的；payload 来自 `render_inject_with_diagnostics` 返回的 `rendered_items`（即经 budget / 去重过滤后实际渲染到 `<limem_memory>` 内的条目）。daemon 收到后：① 写入 `DaemonState.recent_recalls` 环形 deque（`runtime.recent_recalls_max` 默认 20）+ 更新 `last_recall` 摘要；② 把同一 record 标记为 `pending_recall_by_session[session_id]` 等待 Stop hook 消费；③ 同步 emit 一行 `kind="recall_emitted"` 审计到 events.ndjson；④ `statusline_loop` 周期把 `recent_recalls` 原子写到 `~/.cache/limem/recent_recalls.json` 供冷启动恢复与 daemon 不可达时 fallback。**`list_recent_recalls`** 由 MCP 工具 `limem_recent_recalls` 调用（daemon 不可达时回退读 `recent_recalls.json`）。

**`consume_pending_recall`** 是 Claude Code / Codex Stop hook 用来在回答结束时主动给用户提示的入口：daemon 维护 `session_id → 待消费 record`，每次 `report_recall` 时刷新；Stop hook 取出后 daemon 立即清除 pending 并在 `last_displayed_signature_by_session[session_id]` 记下签名（基于 short_id 集合 + 来源计数的 sha1 前缀），下一轮如果两个签名一致 → 返回 null（静默去重，避免连续两轮提示同样内容）。失败 / pause / 空 items 时 hook 输出空 stdout，对用户不可见。
