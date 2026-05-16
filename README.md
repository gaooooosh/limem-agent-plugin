# limem-agent-plugin

LiMem 长期记忆后端到 **Claude Code** 与 **Codex CLI** 的官方桥接器。
一行命令完成下载、安装、配置、接入，让 AI 编程助手拥有跨会话、跨项目的长期记忆。

---

## 一行安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
```

脚本会按顺序完成：

1. 探测 macOS / Linux / WSL 与 Python ≥ 3.10
2. 自动装 `pipx`（如缺）
3. 从 GitHub 拉源码 → `pipx install limem-cli`
4. `limem init` 自动 patch `~/.claude/settings.json` 与 `~/.codex/config.toml`
5. 进入交互式 `limem bootstrap`，提示输入你的 LiMem API key

### 自动化场景（带 token 一行装完）

```bash
curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh \
  | bash -s -- --api-key sk-xxx
```

或先设置环境变量：

```bash
LIMEM_API_KEY=sk-xxx curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
```

### 其他选项

```bash
curl -fsSL .../install.sh | bash -s -- --help
```

| flag | 说明 |
|---|---|
| `--api-key TOKEN` | 透传到 `limem bootstrap`，跳过交互输入 |
| `--ref REF` | 安装指定分支或 tag（默认 `main`） |
| `--no-init` | 只装 CLI，不 patch Claude/Codex 配置 |
| `--no-bootstrap` | 跳过 bootstrap，自己后续手动跑 |
| `--verbose` | 输出调试信息 |

---

## 手动安装（兜底）

```bash
git clone https://github.com/gaooooosh/limem-agent-plugin.git
cd limem-agent-plugin
pipx install ./limem-cli
limem init                                     # 全局配置 Claude Code + Codex
limem bootstrap --api-key <YOUR_API_KEY>       # 验证 token + 解析/创建 db
```

---

## 平台支持

- ✅ macOS（Intel / Apple Silicon，需 Python 3.10+，推荐 `brew install python@3.12`）
- ✅ Linux（Debian/Ubuntu/Fedora/Arch 等，需 `python3` ≥ 3.10）
- ✅ WSL（与 Linux 一致；`~/.claude` 在 WSL 文件系统内）
- ❌ Windows 原生（请用 WSL）

---

## 核心能力

| 模块 | 作用 |
|---|---|
| **MCP server** (`limem-mcp`) | 注册 `limem_search` / `limem_write` / `limem_forget` / `limem_list` / `limem_ping` 等工具，被 Claude/Codex 通过 stdio 自动拉起 |
| **Hooks** | `UserPromptSubmit` 三层召回（pattern → soft → hard）；`SessionStart/Stop/PreCompact` 自动落档 |
| **Skills** | `/limem.remember`、`/limem.recall`、`/limem.forget`、`/limem.fix`、`/limem.no` 等 slash 命令 |
| **Pattern Index** | 本地 SQLite FTS5 trigram 索引，离线高速命中已知规则 |
| **DB 策略** | 每用户一个 db，项目隔离靠 `event_metadata.scope` 逻辑过滤；`db_id` 完全由 CLI 内部管理 |

---

## 常用命令

```bash
limem bootstrap --api-key <TOKEN>   # 接入：解析或创建唯一 db，落盘凭证（chmod 600）
limem init                          # 全局 patch Claude Code + Codex 配置
limem init --project                # 项目级：写 .limem/local.json + .gitignore
limem ping                          # 检测后端连通 + key 有效 + db 可达
limem info                          # 打印当前凭证（api_key 自动脱敏）
limem db list                       # 列出当前用户名下所有 db
limem db use DB_ID                  # 切换 active db
limem db new NAME [--use]           # 新建 db（多设备隔离场景）
limem stats                         # 本地 SQLite 缓存统计
```

完整子命令见 `limem --help`。

---

## 更多文档

- **CLI 详细文档**：[limem-cli/README.md](limem-cli/README.md)
- **架构与开发约定**：[CLAUDE.md](CLAUDE.md)
- **产品需求文档**：[docs/prd-unobtrusive-memory.md](docs/prd-unobtrusive-memory.md)

---

## 安全约定

- 凭证**只**写 `~/.config/limem/credentials.json`（chmod 600），绝不进任何工具配置或项目目录
- Hook 异常一律 swallow + log，永不阻塞用户 prompt
- 写入侧自动 redact API key / private key / Bearer token

---

## License

MIT © 2026 gaooooosh
