# LiMem Agent Plugin

LiMem Agent Plugin 是 LiMem 长期记忆服务面向 **Claude Code** 与 **Codex CLI** 的 Agent 接入插件。

它把一次性的 AI 编程会话升级为可持续协作关系：项目约定、用户偏好、反复纠正、团队规范、运维流程、服务上下文和 Agent 行为要求，都可以跨会话、跨项目地记住、召回、编辑、静音、归档、导出和治理。

> 这个仓库本身不是一个独立记忆后端，而是依托 LiMem 服务运行的客户端插件与本地运行时。

---

## 项目定位

大多数编码 Agent 在会话结束后都会遗忘关键上下文，而真实工程协作依赖大量稳定但分散的知识：

- “这个项目不要启动本地 dev server，修改后直接重建 Docker。”
- “评审 analytics 问题时必须验证写入路径、后端数据和页面渲染。”
- “部署前先检查服务健康、容器状态和公开访问路径。”
- “LiMem 服务日志不能耦合进算法模块。”
- “用户偏好直接、可执行、有证据的工程反馈。”

LiMem Agent Plugin 提供的是一个面向 Agent 的 LiMem 接入层。它通过 MCP、Hooks、Slash Skills、本地 Daemon 和 SQLite 镜像，把 LiMem 后端能力接入编码工具，同时保证凭证安全、召回可控、上下文不被无关信息污染。

---

## 核心特性

| 能力 | 说明 |
|---|---|
| 长期记忆写入 | 持久化规则、反馈、偏好、事实、笔记、决策和操作经验。 |
| 跨会话召回 | 在用户 prompt 进入 Agent 前自动召回当前项目相关记忆。 |
| Claude Code + Codex 支持 | 同一套运行时同时支持 Claude Code 插件与 Codex MCP 集成。 |
| MCP 工具集 | 暴露搜索、写入、列表、归档、修订、暂停、静音、统计、principal 和 pattern 管理能力。 |
| Slash Skills | 内置 `/limem.*` 技能，方便 Agent 在对话中显式操作记忆。 |
| Principal 档案 | 支持 `user`、`agent`、`project`、`team`、`service` 等主体的 Markdown 档案。 |
| 三层召回 | 本地强规则召回、Principal Pattern 召回、LiMem 软召回协同工作。 |
| 被动学习 | 从重复纠正和被接受的工具行为中生成候选建议，用户审阅后再入库。 |
| 本地 Daemon | 将慢任务、状态、队列、学习分析和连通性检查移出 prompt 同步路径。 |
| 状态行与 TUI | 通过 statusline 和 `limem dash` 展示健康状态、召回次数、暂停状态和候选建议。 |
| 安全边界 | 凭证隔离、敏感信息 redaction、hook 异常吞掉并记录，避免阻塞用户工作流。 |

---

## 设计特点

### 1. 记忆有作用域，不制造全局噪声

LiMem 区分全局用户偏好和项目级规则。插件会识别当前仓库，并按 `global` 与 `project:<id>` 过滤召回结果，避免一个项目的规则污染另一个项目。

### 2. 用户始终拥有控制权

记忆不是黑盒。用户可以写入、列表、搜索、修订、静音、暂停、归档和导出记忆。每条被召回的记忆都带有短 ID，方便在对话中快速 `/limem.fix` 或 `/limem.no`。

### 3. 面向编码 Agent 的低延迟设计

Hook 路径保持轻量和可降级。重任务交给 `limemd` 后台处理；召回有独立预算和超时；本地 SQLite 镜像用于权威 scope/type 过滤，减少后端搜索噪声。

### 4. 不污染 Agent 主上下文

插件只把真正的召回结果和极简降级状态注入 Agent 上下文。建议、统计、日志、调试信息走 statusline、TUI、本地通知和本地文件，不把“我注意到……”这类内容塞进主对话。

### 5. 与 LiMem 后端职责清晰

插件侧负责本地安装、Agent 配置、召回注入、显式写入、被动学习候选和本地缓存；长期存储、搜索、图谱和多租户能力由 LiMem 后端提供。

---

## 功能场景

### 个人 AI 编程记忆

- 记录个人编码偏好和命令习惯
- 记住某个项目的特殊运行方式
- 避免 Agent 反复犯同类错误
- 跨会话保留调试、部署和验证经验
- 在新会话中自动恢复关键上下文

### 团队 Agent 治理

- 沉淀仓库级工程规范
- 统一 Review 和测试检查标准
- 记录服务边界、模块职责和部署流程
- 为不同团队、服务、项目维护独立档案
- 让 Agent 遵守团队已有工作方式

### LiMem 客户端接入层

- 为开发者提供标准化 LiMem 安装入口
- 将 LiMem 记忆能力接入 Claude Code 和 Codex
- 通过 MCP 工具开放可编程操作面
- 支持本地诊断、导出和基础审计
- 让用户偏好、项目规范和服务知识沉淀到 LiMem

---

## 架构概览

```text
Claude Code / Codex
        |
        | hooks + MCP tools + slash skills
        v
limem-cli runtime
        |
        | local SQLite mirror, scope filtering, short IDs
        |
        +---- limemd daemon
        |       | event bus, passive learning, statusline cache,
        |       | suggestions queue, connectivity state
        |
        v
LiMem backend
        |
        | /db/{db_id}/...
        v
Long-term memory graph and search service
```

关键边界：

- `limem.client` 是唯一直接耦合 LiMem 后端 HTTP 契约的模块。
- Hooks 失败必须吞掉并记录，不能阻塞用户 prompt。
- 凭证只写入 `~/.config/limem/credentials.json`，权限为 `chmod 600`。
- 项目配置写入 `.limem/local.json`，并加入 `.gitignore`。
- 被动学习只生成候选建议，用户确认后才成为长期记忆。

---

## 安装

### 一行安装

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
```

安装脚本会自动完成：

1. 检测 macOS / Linux / WSL。
2. 验证 Python 3.10+。
3. 安装或复用 `pipx`。
4. 安装 `limem-cli`。
5. Patch Claude Code 与 Codex 配置，接入 hooks、MCP server、statusline 和 skills。
6. 进入 `limem bootstrap`，配置 LiMem API Key 与数据库。

### 非交互安装

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh \
  | bash -s -- --api-key sk-xxx
```

也可以使用环境变量：

```bash
LIMEM_API_KEY=sk-xxx \
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
```

### 安装参数

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh \
  | bash -s -- --help
```

| 参数 | 说明 |
|---|---|
| `--api-key TOKEN` | 传给 `limem bootstrap`，跳过交互输入。 |
| `--ref REF` | 安装指定分支或 tag，默认 `main`。 |
| `--no-init` | 只安装 CLI，不 patch Claude Code / Codex 配置。 |
| `--no-bootstrap` | 跳过 LiMem API Key 初始化。 |
| `--verbose` | 输出安装调试信息。 |

---

## 手动安装

```bash
git clone https://github.com/gaooooosh/limem-agent-plugin.git
cd limem-agent-plugin
pipx install ./limem-cli
limem bootstrap --api-key <YOUR_API_KEY>
limem init
```

为当前项目建立 project scope：

```bash
cd your-project
limem init --project
```

---

## 平台支持

| 平台 | 状态 |
|---|---|
| macOS Intel / Apple Silicon | 支持 |
| Linux | 支持 |
| WSL | 支持 |
| Windows 原生 shell | 暂不支持，请使用 WSL |

要求 Python 3.10+。

---

## 常用命令

```bash
limem ping
limem info
limem stats
limem remember "这个项目修改后总是重建 Docker，不要启动本地 dev server"
limem export --format markdown
```

```bash
limem pattern get project
limem pattern put project ./PROJECT_MEMORY.md
limem entity list
limem daemon status
limem dash
```

---

## Slash Skills

插件内置一组可被 Agent 使用的 `/limem.*` 技能：

| Skill | 作用 |
|---|---|
| `/limem.remember` | 保存规则、偏好、事实、决策、笔记或反馈。 |
| `/limem.recall` | 手动搜索 LiMem 记忆。 |
| `/limem.list` | 列出当前项目和全局的规则、反馈、偏好。 |
| `/limem.fix` | 按短 ID 或 event ID 原地修订一条记忆。 |
| `/limem.no` | 在当前会话临时静音某条召回记忆。 |
| `/limem.forget` | 归档一条长期记忆。 |
| `/limem.pause` | 暂停召回与采集。 |
| `/limem.resume` | 恢复召回与采集。 |
| `/limem.pattern` | 查看或更新 principal Markdown 档案。 |
| `/limem.entity` | 管理 user、agent、project、team、service 等 principal。 |
| `/limem.note` | 保存低优先级笔记。 |
| `/limem.feedback` | 保存针对 Agent 行为的纠正反馈。 |
| `/limem.stats` | 查看本地记忆缓存统计。 |

---

## MCP 工具

`limem-mcp` 通过 stdio 提供结构化 MCP server。

| 领域 | 工具 |
|---|---|
| 查询 | `limem_search`, `limem_list` |
| 写入 | `limem_write`, `limem_forget`, `limem_fix` |
| 会话控制 | `limem_pause`, `limem_resume`, `limem_mute` |
| 诊断 | `limem_ping`, `limem_stats` |
| Principal 档案 | `limem_pattern_get`, `limem_pattern_put`, `limem_pattern_delete` |
| Principal 管理 | `limem_principal_list`, `limem_principal_register`, `limem_principal_activate`, `limem_principal_deactivate` |

MCP 层复用 CLI、Hooks 和写入模块的业务逻辑，不重复拼接后端 HTTP 请求。

---

## 召回模型

LiMem Agent Plugin 使用三层召回：

1. **Hard recall**：本地 SQLite 中的高优先级规则、反馈和偏好。
2. **Pattern recall**：LiMem 后端对 principal Markdown 档案做 H2 section 匹配。
3. **Soft recall**：LiMem 后端搜索结果，再由本地镜像按 scope、type、principal 做权威过滤。

注入块保持紧凑，并带有来源、预算、项目标识和短 ID：

```xml
<limem_memory recall="2" via="project:docker | bm25:rebuild" budget="420/2000" project="github.com/org/repo">
...
</limem_memory>
```

---

## 被动学习

插件可以通过 hooks 收集重复纠正和被接受的工具行为，再由 `limemd` 聚合成候选建议。

设计原则是“建议可见，但不自动污染长期记忆”：

- Hook 只追加轻量事件。
- Daemon 在后台聚合信号。
- 候选写入本地 suggestions 队列。
- 用户通过 `limem dash` 接受、编辑或丢弃。
- 只有被确认的建议才会写入 LiMem。

---

## 安全与隐私

- API Key 只保存到 `~/.config/limem/credentials.json`。
- 凭证文件使用 owner-only 权限。
- Claude Code / Codex 配置中不写入任何密钥。
- 写入记忆前会拦截 API key、private key、Bearer token 等敏感信息。
- Hook 异常会记录日志并吞掉，不能影响用户 prompt。
- 项目级本地配置默认加入 `.gitignore`。
- 支持会话级静音和暂停，适合敏感任务。
- 支持导出，便于审计、迁移和备份。

---

## 开发

```bash
git clone https://github.com/gaooooosh/limem-agent-plugin.git
cd limem-agent-plugin
python -m venv .venv
source .venv/bin/activate
pip install -e './limem-cli[dev]'
```

运行检查：

```bash
cd limem-cli
ruff check .
pytest
```

调试 hooks：

```bash
echo '{"prompt":"记住这个项目规则"}' | limem hook claude-code UserPromptSubmit
echo '{}' | limem hook codex SessionStart
limem daemon start --foreground
limem daemon tail --from-start
```

---

## 仓库结构

```text
.
├── install.sh
├── limem-cli/
│   ├── limem/
│   │   ├── cli.py
│   │   ├── client.py
│   │   ├── hooks.py
│   │   ├── mcp_server.py
│   │   ├── memory_writer.py
│   │   ├── entity_index.py
│   │   └── daemon/
│   └── tests/
├── plugin-src/
│   ├── .claude-plugin/
│   ├── .codex-plugin/
│   ├── hooks/
│   └── skills/
└── docs/
```

---

## 更多文档

- CLI 详细说明：[limem-cli/README.md](limem-cli/README.md)
- Agent 开发约定：[CLAUDE.md](CLAUDE.md)
- 被动学习 PRD：[docs/prd-unobtrusive-memory.md](docs/prd-unobtrusive-memory.md)

---

## License

MIT © 2026 gaooooosh
