# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库定位

LiMem agent-plugin：把 LiMem 长期记忆后端（多租户 FastAPI）桥接到 **Claude Code** 与 **Codex CLI**。仓库产出一个 Python 包 `limem-cli`（提供 `limem` 与 `limem-mcp` 两个 entry-point），以及一份可被两边复用的 plugin source（skills + hooks + plugin.json）。

仓库根布局：

- `limem-cli/` — Python 源码（hatchling 构建，pipx 安装），所有运行时逻辑在 `limem-cli/limem/`
- `limem-cli/limem/daemon/` — 后台 daemon (`limemd`) 子模块，承载异步写入、被动学习、连通性状态机、suggestions 队列、statusline 缓存
- `plugin-src/.claude-plugin/plugin.json` 与 `plugin-src/.codex-plugin/plugin.json` — 两个工具的 plugin manifest
- `plugin-src/skills/limem.*/SKILL.md` — 用户可调用的 slash skill（13 个：`remember` / `recall` / `forget` / `fix` / `no` / `note` / `feedback` / `list` / `stats` / `pause` / `resume` / `entity` / `pattern`）
- `plugin-src/hooks/hooks.json` — Codex 的 hook 配置（Claude Code 直接读 plugin.json 内嵌的 hooks）
- `docs/prd-unobtrusive-memory.md` — 演进 PRD（external-first / daemon / suggestions / statusline 设计依据）
- `.spec-workflow/` — spec-workflow 工作区（未承载实际代码）

## 常用命令

### 开发环境

```bash
# 在仓库根用 uv venv（.venv 已就位）或 pipx 装 limem-cli
pipx install ./limem-cli                # 推荐：把 limem / limem-mcp 暴露到 PATH
# 或本地开发
pip install -e ./limem-cli[dev]
```

### 安装到 Claude Code / Codex

```bash
limem bootstrap --api-key <YOUR_API_KEY>   # 用 LiMem dashboard 拿到的 user key 接入；自动解析或创建唯一 db
limem init                                 # 全局：patch ~/.claude/settings.json + ~/.codex/config.toml，铺 skills
limem init --project                       # 项目级：仅写 .limem/local.json + .gitignore（不再改 AGENTS.md/CLAUDE.md）
limem ping                                 # 校验后端连通 + key 有效 + db_id 可达
limem stats                                # 本地 SQLite 缓存计数（principals + event_metadata）
limem info                                 # 显示当前凭证（api_key 脱敏）
limem db list / use DB_ID / new NAME       # 多 db 管理（默认策略=每用户一个 db；大多数用户用不到）
```

> **DB 策略**：每个用户一个 db，所有项目共用；project 级隔离靠 `event_metadata.scope` 字段（`global` / `project:<id>`）逻辑划分，召回时按 `allowed_scopes` 过滤。`db_id` 完全由 `limem-cli` 内部管理，用户不需要、也不应该手填。

### 写入 / 召回 / 维护

```bash
limem remember "禁止 npm run dev，用 docker rebuild"  # 显式写入（也可通过 /limem.remember skill）
limem pattern get|put|delete <principal-id|alias>     # principal markdown 档案 CRUD
limem entity list|register|activate|deactivate        # principal 管理（user/agent/project/team/service）
limem export [--format json|markdown]                 # 整库导出（不污染召回缓存）
limem sync-static [--target AGENTS.md]                # 用户主动调用才生成静态镜像；installer 不再自动生成
```

### Daemon

```bash
limem daemon start [--foreground]    # 拉起 limemd（unix socket: ~/.cache/limem/limemd.sock）
limem daemon status                  # 当前 PID / 连通性 / 队列长度
limem daemon stop                    # 优雅关停
limem daemon tail [--from-start]     # 跟踪 events.ndjson
limem daemon reset [--cache]         # 清 suggestions / 缓存
limem statusline                     # stdout 单行状态串（被 Claude statusLine 调用，< 50ms）
limem dash [--logs]                  # TUI 面板（候选审阅、日志、统计）
```

### Hook 手动调试

```bash
echo '{"prompt":"起一下 dev"}' | limem hook claude-code UserPromptSubmit
echo '{}' | limem hook codex SessionStart
```

调试日志在 `~/.cache/limem/hooks.log`（JSONL）；事件总线（daemon 输入队列）在 `~/.cache/limem/events.ndjson`。

### MCP server

直接 `limem-mcp` 在 stdio 上启动；通常由 Claude Code/Codex 通过 `mcpServers` 配置自动拉起。

### 代码质量

```bash
cd limem-cli
ruff check .                            # 配置见 pyproject.toml：line-length=100, target=py310, select E,F,I,W,B,UP
mypy limem                              # 可选
pytest                                  # tests/ 现有 6 个用例（bootstrap / hooks 召回 / entity_index 重建 / principal alias 解析 / remember 不再注册 entity / smoke / manifest sync）
```

## 架构要点

### 唯一后端契约耦合点：`limem/client.py`

后端是 LiMem 多租户 FastAPI（`https://limem.gaooooosh.art`），所有数据操作走 `/db/{db_id}/...`，鉴权头 `X-API-Key`。**只有 `LimemClient` 这一个模块直接拼接 HTTP 请求**；其他模块（hooks、mcp_server、memory_writer、daemon/writer、cli）只能调它。改动后端契约只需改 `client.py`。

关键约束（写新代码前必读 `client.py` 顶部 docstring）：
- `ingest` body 必须形如 `{data: {...}, timestamp: int}`，所有 metadata（`limem_scope`、`limem_type`、`importance` 等）塞在 `data` 内
- `query` body 是 `{query, top_k}`，**不接 filters**；scope/type 过滤必须**客户端**做（见 `EntityIndex.filter_query_results`）
- `query` 返回的 `summary` 是 LLM 生成的纯文本，**不含原始 metadata**——这是为什么需要本地 `event_metadata` 镜像
- `patterns_recall` 是 v3 的 principal 召回端点：传 principal entity_ids 与 hints，返回每个 principal 的 markdown 档案命中的 H2 切片
- `entity_create_or_promote` / `entity_patch` / `graph_update_event` 是 principal 与 event 的写入端点

### v3 架构：Principal-centric

> v2 时代每个 mention（命令 / 路径 / 术语）都注册为后端 entity；v3 改为只在少量 **principal**（user / agent / project / team / service）上挂 **markdown 档案**，召回按 H2 标题切片。

- **默认 principal**：SessionStart 时 `principals.ensure_default_principals` 幂等地确保 `user / agent / project` 三个在后端和本地都注册（落标记到 `~/.cache/limem/principals_ensured/<eid>`）。
- **稳定 ID**：`principal_user_<sha8>` / `principal_agent_<slug>` / `principal_project_<sha8>` —— 见 `principals.entity_id_for`。
- **CLI/MCP 别名解析**：`principals.principal_alias_to_id` 把 `"user"` / `"agent"` / `"project"` 等易记字面量解析为 stable entity_id（CLI/MCP 层调用 `update_pattern` / `get_pattern` 之前必须先解析）。

### 三层召回（UserPromptSubmit 注入的核心）

`limem/hooks.py::_hook_user_prompt_submit` 在每条 prompt 进来时用 `ThreadPoolExecutor(max_workers=3)` 并发跑：

1. **Hard 召回**（`EntityIndex.list_hard_recall`）：本地 SQLite 中匹配 scope 的 `rule / feedback / preference` 事件，离线高速。
2. **Pattern 召回**（`LimemClient.patterns_recall`）：对每个活跃 principal 并发请求后端，返回 markdown 档案按 H2 命中的切片。每 principal 独立超时 `patterns_recall_timeout_ms`（默认 80ms）。
3. **Soft 召回**（`LimemClient.query` BM25）：把 prompt 原文（v3 不再追加 hint token，参见 `tag_text.build_recall_query` docstring）发后端语义搜索；结果必须通过 `EntityIndex.filter_query_results` 用本地 `event_metadata` 做权威 scope/type 过滤，**未在本地缓存的 event 直接丢弃**（无法判断 scope 即不注入）。

三层结果汇到 `injector.render_inject` → 渲染为 `<limem_memory recall="N" via="..." budget="X/Y" project="...">...</limem_memory>` 区块：

- **三段独立预算**：`runtime.inject_budget_hard` / `inject_budget_pattern` / `inject_budget_soft` 物理分离，互不挤压。
- **时间衰减**：`injector._half_life_score = importance × 0.9 ** 月数` 用于 hard/soft 排序；pattern 切片用后端 `matched_sections` 总分。
- **via header**：列出本轮命中的 pattern triggers 与 BM25 top 关键词（`_via_keywords`）。
- **short_id**：每条记忆末尾追加 `#xxxx`（12 位 sha1 前缀，冲突时扩到 14 位），存 `EntityIndex.ensure_short_id` → `short_id_map` 表，供 `/limem.fix` 与 `/limem.no` 引用。

### 本地 SQLite 镜像（`limem/entity_index.py`）

> **历史注意**：`limem/pattern_index.py` 自 v2 起仅是 deprecated 兼容层（`PatternIndex = EntityIndex` 别名）；FTS5 trigram 索引已废弃，主体迁到 `EntityIndex`。

SQLite 数据库 `~/.cache/limem/patterns.sqlite`，schema 由 `_ensure_schema` 维护版本（`SCHEMA_VERSION`），不兼容时自动 rebuild：

- `principals` — 每个 principal 一行：`entity_id` / `principal_type` / `slug` / `canonical` / `aliases` / `description` / `scope` / `tool` / `project_id` / `active` / `has_pattern` / `last_seen_ts` / `raw_metadata`
- `event_metadata` — 每个 event 一行：`event_id` / `scope` / `mem_type` / `importance` / `ts` / `principal_ids` / `canonicals` / `tombstone` / `raw_metadata`
- `short_id_map` — `short_id` ↔ `event_id` 双向映射，用于 `/limem.fix #xxxx`

**软删除（`tombstone=1`）而非 DELETE**——为了让 `/limem.forget` 能撤销。所有查询都过滤 `tombstone=0`。写后端的同时镜像到本地，这是 `daemon/writer.remember_impl` 的职责。

### Scope 识别（`limem/scope.py`）

每条记忆挂在一个 scope 上：`global` 或 `project:<id>`。项目 id 按优先级取：
1. `.limem/local.json` 显式 `project_id`
2. `git remote get-url origin` 规范化（`normalize_git_remote`）
3. `package.json#name` / `pyproject.toml#project.name` / `Cargo.toml#package.name`
4. cwd basename + sha1(abs_path)[:8]

Hook 与 MCP server 召回时 `allowed_scopes = ["global", f"project:{project_id}"]`。

### Tag-as-token 策略（`limem/tag_text.py`）

写入侧：`encode_tags(scope=..., type=..., canonical=..., principal=...)` 把 metadata 序列化为 `[limem.scope=project:foo]` token 嵌入 event 文本，BM25 同时索引内容与 metadata token。**查询侧 v3 起不再追加 hint token**（`build_recall_query` 现仅返回 prompt 原文）——baseline 噪声大，权威过滤完全交给 `EntityIndex.filter_query_results` 用本地镜像完成。`extract_tags` / `matches_scope` / `filter_by_scope_and_type` 仅在需要从 BM25 命中文本反向解析 tag 时用。

### 安装器（`limem/installer.py`）的合并语义

`patch_claude_settings` 与 `patch_codex_config` **绝不覆盖**用户既有 hooks/mcpServers/statusLine，只追加 limem 自己的命名段。每个事件下查重逻辑认两种格式：嵌套 `{hooks:[{command}]}` 与扁平 `{command}`。写入前自动备份 `.limem-bak`。凭证**永远**写 `~/.config/limem/credentials.json`（chmod 600），绝不进 settings.json 或项目目录。

**阶段 2 变更**（已落地，对应 PRD F10）：`project_init` 不再修改 `AGENTS.md` / `CLAUDE.md`；只写 `.limem/local.json` + `.gitignore` 追加。用户主动调 `limem sync-static` 才生成静态镜像。历史项目的占位块可用 `limem migrate clean-static` 一次性清理。

Codex 没有 `SessionEnd` 事件——`_hook_stop_codex` 用 ndjson 缓冲池模拟：每次 Stop append 到 `~/.cache/limem/sessions/<sid>.ndjson`，文件 mtime 超过 `runtime.codex_stop_idle_seconds`（默认 30s）后整体 flush 为一条 `session_summary` ingest（`_flush_codex_session`）。

### 写入流（`limem/memory_writer.py` → `limem/daemon/writer.py`）

`memory_writer` 是**客户端薄层**，所有真实写入业务在 `daemon/writer.py`。`remember()` 是核心入口（被 MCP `limem_write` 工具、`/limem.remember` skill、`limem remember` CLI 共享）：

1. 优先走 daemon RPC（`daemon_client.write_memory`）；daemon 不可达时直接调 `remember_impl` 同步路径。
2. `remember_impl` 流程：
   - `redact.contains_secret` 拦截 API key / private key / Bearer token（除非 `skip_redact=True`）
   - `tag_text.encode_tags` 把 scope/type/canonical/principal 编码进文本，再 `client.ingest`
   - `principals.ensure_default_principals` 幂等保证 user/agent/project 三个 principal 已注册到后端与本地
   - **v3 不再 `register_entity` 每个 mention**——`entities` 参数仅作为本地 `canonicals` / `mentions` 镜像存到 `raw_metadata`
   - 同步镜像 `event_metadata` 到本地 SQLite

`RememberResult` 返回 `event_id` / `summary` / `scope` / `principal_ids` / `canonicals`；旧字段 `entity_ids` / `pattern_count` 仍保留（指向 principal_ids）为兼容旧 MCP/CLI 调用方。

其它写入路径：
- `forget(event_id)` → 后端 `event_archive` + 本地 `tombstone_event`
- `fix(event_id, new_text)` → 后端 `graph_update_event`（**禁止产生新 event_id**，避免历史链断裂）+ 本地 metadata 同步
- `update_pattern(entity_id, content)` / `get_pattern` / `delete_pattern` —— principal markdown 档案 CRUD，**不走 daemon**（操作幂等且短）

### MCP 工具（`limem/mcp_server.py`）

注册 16 个工具，按域分组：

| 域 | 工具 |
|---|---|
| 查询 | `limem_search` / `limem_list` |
| 写入 | `limem_write` / `limem_forget` / `limem_fix` |
| 会话控制 | `limem_pause` / `limem_resume` / `limem_mute` |
| 诊断 | `limem_ping` / `limem_stats` |
| Pattern 档案 | `limem_pattern_get` / `limem_pattern_put` / `limem_pattern_delete` |
| Principal 管理 | `limem_principal_list` / `limem_principal_register` / `limem_principal_activate` / `limem_principal_deactivate` |

所有工具都直接调 `memory_writer` 或 `entity_index`，**不复制业务逻辑**、**不直接拼 HTTP**。

### Daemon 子系统（`limem/daemon/`）

`limemd` 是可选的长生命周期 daemon（unix socket `~/.cache/limem/limemd.sock`，单实例锁 `limemd.pid`）。设计参考 `docs/prd-unobtrusive-memory.md`。当前职责：

- **writer**：实际执行 `remember/forget/fix` 与 principal markdown 更新，承担后端 IO 耗时
- **eventbus**：消费 `~/.cache/limem/events.ndjson`（hook 写入端，daemon 读取端）
- **connectivity**：维护后端连通性状态机（连续 3 次 401/403/5xx → `degraded`；恢复 1 次 → `healthy`）
- **learner**：被动学习骨架（重复纠正聚合、N-gram 频次统计；suggestions 写 `~/.cache/limem/suggestions.json`，由 `limem dash` 审阅）
- **rpc**：line-delimited JSON over unix socket，方法见 PRD 附录 C
- **auto_init**：hook 检测 socket 无响应时静默 fork 拉起 daemon
- **lock / state / ngram / jaccard**：单实例锁、daemon 状态序列化、相似度算子

> 注意：daemon 失败时所有写入/召回**自动 fallback 到同步路径**，daemon 不可用永远不能阻塞 hook。

## 项目约定

- **永远不要把凭证写进任一工具的配置或项目目录**——只能在 `~/.config/limem/credentials.json`（chmod 600）。
- 改 hooks schema 时同步改三处：`plugin-src/.claude-plugin/plugin.json`、`plugin-src/hooks/hooks.json`、`limem/installer.py::_CLAUDE_HOOKS` 与 `patch_codex_config` 内嵌的 dict。
- Hook 异常必须 **swallow + log**（见 `hooks.py::main` 的 try/except）；hook 失败永远不能阻塞用户 prompt。
- 加新 MCP 工具时：在 `mcp_server._list_tools` 加 `Tool(...)` 描述，在 `_call_tool` 加分支，业务逻辑放 `memory_writer` / `entity_index` / `daemon/writer`，不在 `mcp_server` 里写直接的 HTTP 调用。
- 加新写入流程时：先在 `daemon/writer.py` 落 `*_impl`，然后在 `memory_writer.py` 加薄层包装（daemon RPC 优先 + 同步 fallback）。
- Codex 与 Claude Code 共用同一份 `plugin-src/skills/`；SKILL.md 顶部 frontmatter 字段（name/description/arguments）必须两边都兼容。
- **不要向 `additionalContext` 注入非召回类文本**（"我注意到 ..." / "建议保存 ..." / 错误堆栈）；提议、统计、告警走 statusline + TUI dash + 本地通知三条外置通道（PRD 设计原则 P2）。

## 测试与契约校验

`tests/` 现有用例（覆盖核心路径，但缺 e2e）：

- `test_bootstrap_user_session.py` — bootstrap 多 db 选择流程
- `test_hooks_pattern_recall.py` — UserPromptSubmit 三层并发召回
- `test_entity_index_v3_rebuild.py` — schema 版本升级与 rebuild
- `test_principal_alias_resolver.py` — `"user/agent/project"` → entity_id
- `test_remember_no_entity_registration.py` — v3 不再 register_entity（防回归）
- `test_manifest_sync.py` — plugin.json / installer hook 配置三处一致
- `test_smoke_imports.py` — 顶层符号 smoke

后端契约（schema、端点路径）通过生产 `/openapi.json` 验证，校验日期记在 `client.py` 顶部 docstring。改契约后请更新这个日期与对应说明。
