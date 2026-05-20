# limem-cli

LiMem long-term memory bridge for Claude Code and Codex CLI.

提供：
- 本地 MCP stdio server，注册 `limem_search` / `limem_write` / `limem_forget` / `limem_list` / `limem_promote` / `limem_tune` / `limem_ping`
- Hook 调度器，支持 Claude Code 与 Codex 的 `UserPromptSubmit` / `SessionStart` / `SessionEnd` / `Stop` / `PreCompact` 事件
- `limem` CLI：安装器、Pattern Index 同步、状态查看、健康检查
- 与 LiMem 后端（多租户 FastAPI，端点 `/db/{db_id}/...`）的唯一对接层

完整设计见 [plan 文件](../../.claude/plans/skills-claude-code-codex-skills-hook-qu-buzzing-lampson.md)。

## 快速使用

```bash
uv tool install --force ./limem-cli
limem init                 # 全局安装到 Claude Code + Codex
cd your-project && limem init --project
```

详见 `limem --help`。
