"""断言四处 hook 配置的命令集合一致——避免单边漂移。

四处：
1. plugin-src/.claude-plugin/plugin.json#hooks
2. plugin-src/hooks/hooks.json（Codex 用）
3. installer._CLAUDE_HOOKS（Claude Code 内嵌 dict）
4. installer._CODEX_HOOKS（Codex 内嵌 dict）

允许的差异（写死）：
- Claude Code 有 SessionEnd / PreCompact / PostToolUse；Codex 没有
- Codex 有 Stop；Claude Code 没有
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

CLAUDE_PLUGIN_JSON = ROOT / "plugin-src" / ".claude-plugin" / "plugin.json"
CODEX_HOOKS_JSON = ROOT / "plugin-src" / "hooks" / "hooks.json"

# Claude Code 期望事件集合
CLAUDE_EXPECTED = {
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "PreCompact",
    "PostToolUse",
}
# Codex 期望事件集合（hooks.json + installer 都该一致）
CODEX_EXPECTED = {"UserPromptSubmit", "SessionStart", "Stop"}


def _extract_commands(hook_block: dict) -> dict[str, set[str]]:
    """返回 event → {command, ...} 集合。"""
    out: dict[str, set[str]] = {}
    for event, entries in hook_block.items():
        cmds: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict) and "hooks" in entry:
                for h in entry["hooks"]:
                    cmd = h.get("command")
                    if cmd:
                        cmds.add(cmd)
            elif isinstance(entry, dict) and "command" in entry:
                cmds.add(entry["command"])
        out[event] = cmds
    return out


def test_claude_plugin_json_events() -> None:
    data = json.loads(CLAUDE_PLUGIN_JSON.read_text())
    assert set(data["hooks"].keys()) == CLAUDE_EXPECTED


def test_codex_hooks_json_events() -> None:
    data = json.loads(CODEX_HOOKS_JSON.read_text())
    assert set(data.keys()) == CODEX_EXPECTED


def test_installer_claude_hooks_match_plugin_json() -> None:
    from limem.installer import _CLAUDE_HOOKS

    plugin = json.loads(CLAUDE_PLUGIN_JSON.read_text())["hooks"]
    plugin_cmds = _extract_commands(plugin)
    inst_cmds = _extract_commands(_CLAUDE_HOOKS)

    assert set(plugin_cmds.keys()) == set(inst_cmds.keys())
    for event in plugin_cmds:
        assert plugin_cmds[event] == inst_cmds[event], (
            f"mismatch for event {event}: plugin.json={plugin_cmds[event]} "
            f"installer={inst_cmds[event]}"
        )


def test_installer_codex_hooks_match_hooks_json() -> None:
    from limem.installer import _CODEX_HOOKS

    hooks_json = json.loads(CODEX_HOOKS_JSON.read_text())
    hooks_cmds = _extract_commands(hooks_json)
    inst_cmds = _extract_commands(_CODEX_HOOKS)

    assert set(hooks_cmds.keys()) == set(inst_cmds.keys())
    for event in hooks_cmds:
        assert hooks_cmds[event] == inst_cmds[event]


def test_claude_and_codex_share_user_prompt_submit_and_session_start() -> None:
    """两边都必须有 UserPromptSubmit + SessionStart（基础召回入口）。"""
    plugin = json.loads(CLAUDE_PLUGIN_JSON.read_text())["hooks"]
    codex = json.loads(CODEX_HOOKS_JSON.read_text())
    assert "UserPromptSubmit" in plugin
    assert "UserPromptSubmit" in codex
    assert "SessionStart" in plugin
    assert "SessionStart" in codex


def test_post_tool_use_only_in_claude() -> None:
    """PostToolUse 只能出现在 Claude Code（Codex 不支持）。"""
    codex = json.loads(CODEX_HOOKS_JSON.read_text())
    assert "PostToolUse" not in codex
