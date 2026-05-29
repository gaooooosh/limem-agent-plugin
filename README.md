# LiMem Agent Plugin

LiMem Agent Plugin 把 LiMem 长期记忆接入 Claude Code 和 Codex。它提供 CLI、MCP server、hooks、slash skills、本地 daemon 和 SQLite 缓存，让 Agent 能在不同会话里记住项目规则、用户偏好、纠正反馈和服务上下文。

## 安装

推荐只使用这一条命令安装和更新：

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
```

这个命令会自动完成：

- 检测 macOS、Linux 或 WSL 环境。
- 检查 Python 3.10+。
- 安装或更新 `limem` CLI。
- 自动接入本机已有的 Claude Code 和/或 Codex 配置。
- 安装 MCP server、hooks、statusline 和 slash skills。
- 首次使用时引导配置 LiMem API Key 和数据库。

安装完成后，在需要启用项目记忆的仓库里执行：

```bash
limem init --project
```

如果需要重新配置 API Key 或数据库：

```bash
limem bootstrap
```

要求：Python 3.10+。支持 macOS、Linux 和 WSL；Windows 原生 shell 暂不支持。

## 它解决什么问题

编码 Agent 经常在新会话里丢失关键上下文，例如项目启动方式、review 标准、部署流程、用户表达偏好、反复纠正过的错误。LiMem Agent Plugin 把这些信息沉淀成可管理的长期记忆，并在合适的时机注入给 Agent。

适合用来保存：

- 仓库级工程规则和运行方式。
- 用户偏好和沟通风格。
- 代码评审、测试、部署和排障流程。
- 服务边界、模块职责和团队规范。
- Agent 反复犯错后的纠正反馈。

## 核心能力

| 能力 | 说明 |
|---|---|
| 自动召回 | 在 Agent 收到用户 prompt 前召回当前任务相关记忆。 |
| 显式写入 | 通过 CLI、MCP 或 slash skills 保存规则、反馈、事实、笔记和决策。 |
| 项目隔离 | 区分全局用户偏好和项目级记忆，减少跨项目污染。 |
| Principal 档案 | 为 `user`、`agent`、`project`、`team`、`service` 维护 Markdown 档案。 |
| MCP 工具 | 为 Claude Code 和 Codex 暴露结构化记忆操作工具。 |
| Slash Skills | 内置 `/limem.*` 技能，方便在对话中管理记忆。 |
| 本地 daemon | 后台处理状态、队列、学习候选和连通性检查。 |
| 安全边界 | 凭证独立保存，敏感信息写入前 redaction，hook 失败不阻塞工作流。 |

## 常用命令

```bash
limem ping
limem info
limem stats
limem remember "这个项目修改后必须重建 Docker，不要启动本地 dev server"
limem recall "部署流程"
limem list
limem dash
```

Principal 档案：

```bash
limem pattern get project
limem pattern put project ./PROJECT_MEMORY.md
limem entity list
limem project list
```

## Agent 中的用法

安装后，Agent 可以使用 MCP 工具和 slash skills 管理 LiMem 记忆。常用技能包括：

| Skill | 作用 |
|---|---|
| `/limem.remember` | 保存规则、偏好、事实、决策、笔记或反馈。 |
| `/limem.recall` | 手动搜索 LiMem 记忆。 |
| `/limem.list` | 列出当前项目和全局规则。 |
| `/limem.fix` | 按短 ID 修订一条记忆。 |
| `/limem.no` | 当前会话临时静音某条记忆。 |
| `/limem.forget` | 归档一条长期记忆。 |
| `/limem.pause` | 暂停召回与采集。 |
| `/limem.resume` | 恢复召回与采集。 |
| `/limem.pattern` | 查看或更新 principal Markdown 档案。 |
| `/limem.stats` | 查看本地缓存统计。 |

## 工作方式

```text
Claude Code / Codex
        |
        | hooks + MCP tools + slash skills
        v
limem-cli runtime
        |
        | local SQLite cache, scope filtering, short IDs
        |
        +---- limemd daemon
        |       | status, passive learning, suggestions
        |
        v
LiMem backend
        |
        v
Long-term memory graph and search service
```

召回分三层：

1. 本地强规则召回：高优先级规则、反馈和偏好。
2. Principal pattern 召回：匹配 user、agent、project 等 Markdown 档案切片。
3. 任务召回：把当前真实任务交给 LiMem 后端，返回可注入 prompt 的相关记忆。

每条被召回的记忆会带短 ID，方便后续修订、静音或审计。

## 安全与隐私

- API Key 保存到 `~/.config/limem/credentials.json`，并使用 owner-only 权限。
- Claude Code 和 Codex 配置中不写入密钥。
- 写入记忆前会拦截常见 API key、private key、Bearer token 等敏感信息。
- Hook 异常会记录日志并降级，不阻塞用户 prompt。
- 项目级配置写入 `.limem/local.json`，并默认加入 `.gitignore`。
- 支持会话级暂停和静音，适合临时敏感任务。

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

本地调试：

```bash
echo '{"prompt":"记住这个项目规则"}' | limem hook claude-code UserPromptSubmit
echo '{}' | limem hook codex SessionStart
limem daemon start --foreground
limem daemon tail --from-start
```

## 仓库结构

```text
.
├── install.sh
├── limem-cli/
│   ├── limem/
│   └── tests/
├── plugin-src/
│   ├── .claude-plugin/
│   ├── .codex-plugin/
│   ├── hooks/
│   └── skills/
└── docs/
```

## 更多文档

- CLI 详细说明：[limem-cli/README.md](limem-cli/README.md)
- Agent 开发约定：[CLAUDE.md](CLAUDE.md)
- 被动学习 PRD：[docs/prd-unobtrusive-memory.md](docs/prd-unobtrusive-memory.md)

## License

MIT (c) 2026 gaooooosh
