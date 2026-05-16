# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库定位

LiMem agent-plugin：把 LiMem 长期记忆后端（多租户 FastAPI）桥接到 **Claude Code** 与 **Codex CLI**。仓库产出一个 Python 包 `limem-cli`（提供 `limem` 与 `limem-mcp` 两个 entry-point），以及一份可被两边复用的 plugin source（skills + hooks + plugin.json）。

仓库根布局：

- `limem-cli/` — Python 源码（hatchling 构建，pipx 安装），所有运行时逻辑都在 `limem-cli/limem/`
- `plugin-src/.claude-plugin/plugin.json` 与 `plugin-src/.codex-plugin/plugin.json` — 两个工具的 plugin manifest
- `plugin-src/skills/limem.*/SKILL.md` — 用户可调用的 slash skill（`/limem.remember`、`/limem.recall` 等）
- `plugin-src/hooks/hooks.json` — Codex 的 hook 配置（Claude Code 直接读 plugin.json 内嵌的 hooks）
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
limem bootstrap --api-key <YOUR_API_KEY>  # 用 LiMem dashboard 拿到的 user key 接入；自动解析或创建唯一 db
limem init                              # 全局：patch ~/.claude/settings.json + ~/.codex/config.toml，铺 skills
limem init --project                    # 项目级：写 .limem/local.json + .gitignore + AGENTS.md 占位
limem ping                              # 校验后端连通 + key 有效 + db_id 可达
limem stats                             # 本地 patterns.sqlite 缓存计数
limem info                              # 显示当前凭证（api_key 脱敏）
limem db list / use DB_ID / new NAME    # 多 db 管理（默认策略=每用户一个 db；大多数用户用不到）
```

> **DB 策略**：每个用户一个 db，所有项目共用；project 级隔离靠 `event_metadata.scope` 字段（`global` / `project:<id>`）逻辑划分，召回时按 `allowed_scopes` 过滤。`db_id` 完全由 `limem-cli` 内部管理，用户不需要、也不应该手填。

### Hook 手动调试

```bash
echo '{"prompt":"起一下 dev"}' | limem hook claude-code UserPromptSubmit
echo '{}' | limem hook codex SessionStart
```

调试日志在 `~/.cache/limem/hooks.log`（JSONL）。

### MCP server

直接 `limem-mcp` 在 stdio 上启动；通常由 Claude Code/Codex 通过 `mcpServers` 配置自动拉起。

### 代码质量

```bash
cd limem-cli
ruff check .                            # 配置见 pyproject.toml：line-length=100, target=py310, select E,F,I,W,B,UP
mypy limem                              # 可选
pytest                                  # 测试目录 tests/ 当前为空
```

## 架构要点

### 唯一后端契约耦合点：`limem/client.py`

后端是 LiMem 多租户 FastAPI（`https://limem.gaooooosh.art`），所有数据操作走 `/db/{db_id}/...`，鉴权头 `X-API-Key`。**只有 `LimemClient` 这一个模块直接拼接 HTTP 请求**；其他模块（hooks、mcp_server、memory_writer、cli）只能调它。改动后端契约只需改 `client.py`。

关键约束（写新代码前必读 `client.py` 顶部 docstring）：
- `ingest` body 必须形如 `{data: {...}, timestamp: int}`，所有 metadata（`limem_scope`、`limem_type`、`importance` 等）塞在 `data` 内
- `query` body 是 `{query, top_k}`，**不接 filters**；scope/type 过滤必须**客户端**做（见 pattern_index.filter_query_results）
- `query` 返回的 `summary` 是 LLM 生成的纯文本，**不含原始 metadata**——这是为什么需要本地 `event_metadata` 镜像

### 三层召回（UserPromptSubmit 注入的核心）

`limem/hooks.py::_hook_user_prompt_submit` 在每条 prompt 进来时并发跑：

1. **Pattern 召回**（`pattern_index.search_patterns`）：本地 SQLite FTS5 trigram 索引匹配 → 命中即为高置信度规则。FTS5 表达式构造在 `pattern_index._fts5_sanitize`，中文长 token 拆 4 字滑动窗口以解决 trigram 短语过严问题——改这里之前看 docstring。
2. **Soft 召回**（`LimemClient.query` BM25）：把 pattern 命中的 canonical 作为 hints 与 prompt 拼成查询；soft 结果必须通过 `filter_query_results` 用本地 `event_metadata` 做权威 scope/type 过滤，**未在本地缓存的 event 丢弃**（无法判断 scope 即不注入）。
3. **Hard 召回**（SessionStart 用，非 UserPromptSubmit）：直接列本地 `event_metadata` 中匹配的 rule/feedback/preference。

三层结果汇到 `injector.render_inject` → 渲染为 `<limem_memory>...</limem_memory>` 区块，按 `importance × 0.9^月数` 排序，硬上限 `inject_budget_soft + inject_budget_hard` 字节。

### Pattern Index 本地镜像（`limem/pattern_index.py`）

SQLite 数据库 `~/.cache/limem/patterns.sqlite`，两张表：
- `patterns` — 每条 trigger 短语一行；FTS5 虚表 `patterns_fts` 用 **trigram tokenizer** 同时支持中英文子串匹配
- `event_metadata` — 每个 event 一行；scope/type/project/importance/role/raw_metadata（JSON）

**软删除（`tombstone=1`）而非 DELETE**——为了让 `/limem.forget` 能撤销。所有查询都过滤 `tombstone=0`。写后端的同时镜像到本地，这是 `memory_writer.remember` 的职责。

### Scope 识别（`limem/scope.py`）

每条记忆挂在一个 scope 上：`global` 或 `project:<id>`。项目 id 按优先级取：
1. `.limem/local.json` 显式 `project_id`
2. `git remote get-url origin` 规范化（含 `normalize_git_remote`）
3. `package.json#name` / `pyproject.toml#project.name` / `Cargo.toml#package.name`
4. cwd basename + sha1(abs_path)[:8]

Hook 与 MCP server 召回时 `allowed_scopes = ["global", f"project:{project_id}"]`。

### Tag-as-token 策略（`limem/tag_text.py`）

因为后端 query 不支持 filters，写入时把 metadata 编码为 `[limem.scope=project:foo]` 这类 token 嵌入文本，BM25 同时索引内容与 metadata token；召回后再用 `extract_tags` 二次过滤。**已废弃在 query 端注入 scope token**（baseline 噪声大）；当前查询主要靠本地 `event_metadata` 镜像做权威过滤，tag-as-token 仅用于写入侧。

### 安装器（`limem/installer.py`）的合并语义

`patch_claude_settings` 与 `patch_codex_config` **绝不覆盖**用户既有 hooks/mcpServers，只追加 limem 自己的命名段。每个事件下查重逻辑认两种格式：嵌套 `{hooks:[{command}]}` 与扁平 `{command}`。写入前自动备份 `.limem-bak`。凭证**永远**写 `~/.config/limem/credentials.json`，绝不进 settings.json 或项目目录。

Codex 没有 `SessionEnd` 事件——`_hook_stop_codex` 用 ndjson 缓冲池模拟：每次 Stop append 到 `~/.cache/limem/sessions/<sid>.ndjson`，文件 mtime 超过 `codex_stop_idle_seconds`（默认 30s）后整体 flush 为一条 `session_summary` ingest。

### 三件写入流（`limem/memory_writer.py`）

`remember()` 是核心入口（被 MCP `limem_write` 工具与 `limem remember` CLI 共享）：
1. `redact.contains_secret` 拦截 API key / private key / Bearer token
2. `tag_text.encode_tags` 把 scope/type/canonical/patterns 编码进文本，再 `client.ingest`
3. 对每个 entity 调 `client.register_entity`（带 patterns）；409 时 fallback 到 `batch_create_entity_patterns`
4. 同步镜像 `event_metadata` 与 `patterns` 到本地 SQLite

Entity ID 通过 `_stable_entity_id(canonical, role, scope)` 生成（sha1 前 10 位 + slug），保证幂等。

### MCP 工具（`limem/mcp_server.py`）

注册七个工具：`limem_search` / `limem_write` / `limem_forget` / `limem_list` / `limem_ping` / `limem_stats`（另有 `limem_tune` / `limem_recent` 在 docstring 中规划但当前 `_list_tools` 未暴露）。所有工具都同步调用 `memory_writer` 或 `pattern_index`，**不复制业务逻辑**。

## 项目约定

- **永远不要把凭证写进任一工具的配置或项目目录**——只能在 `~/.config/limem/credentials.json`（chmod 600）。
- 改 hooks schema 时同步改三处：`plugin-src/.claude-plugin/plugin.json`、`plugin-src/hooks/hooks.json`、`limem/installer.py::_CLAUDE_HOOKS` 与 `patch_codex_config` 内嵌的 dict。
- Hook 异常必须 **swallow + log**（见 `hooks.py::main` 的 try/except）；hook 失败永远不能阻塞用户 prompt。
- 加新 MCP 工具时：在 `mcp_server._list_tools` 加 `Tool(...)` 描述，在 `_call_tool` 加分支，业务逻辑放 `memory_writer` 或 `pattern_index`，不在 `mcp_server` 里写直接的 HTTP 调用。
- Codex 与 Claude Code 共用同一份 `plugin-src/skills/`；SKILL.md 顶部 frontmatter 字段（name/description/arguments）必须两边都兼容。

## 测试与契约校验

- `tests/` 当前为空；`pyproject.toml` 声明了 pytest + pytest-asyncio 依赖。
- 后端契约（schema、端点路径）通过生产 `/openapi.json` 验证，校验日期记在 `client.py` 顶部 docstring。改契约后请更新这个日期与对应说明。
