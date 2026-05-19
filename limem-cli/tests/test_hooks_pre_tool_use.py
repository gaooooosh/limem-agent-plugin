"""A1.1：_hook_pre_tool_use 行为断言。

- Edit/Write/NotebookEdit 工具：payload 含 intent_summary（new_string head + redact）
- Bash 工具：payload 无 intent_summary（隐私面）
- 任何工具：始终携带 tool + file_path 字段
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import limem.hooks as hooks_mod


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        pre_tool_intent_chars=200,
        redact_patterns=[
            r"\bsk-[A-Za-z0-9]{20,}\b",
            r"\bBearer\s+[A-Za-z0-9._\-]+",
        ],
    )


def _capture_emit(monkeypatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_emit(kind: str, **kwargs: Any) -> None:
        captured.append({"kind": kind, **kwargs})

    monkeypatch.setattr(hooks_mod, "emit_event", fake_emit)
    monkeypatch.setattr(hooks_mod, "detect_project_id", lambda: "demo-proj")
    return captured


def test_pre_tool_use_edit_writes_intent_summary(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    hooks_mod._hook_pre_tool_use(
        tool="claude-code",
        payload={
            "tool_name": "Edit",
            "file_path": "/tmp/foo.py",
            "new_string": "def foo():\n    return 1\n",
            "session_id": "sess-1",
        },
        creds=None,
        runtime=_runtime(),
    )
    assert len(captured) == 1
    evt = captured[0]
    assert evt["kind"] == "pre_tool_use"
    assert evt["payload"]["tool"] == "Edit"
    assert evt["payload"]["file_path"] == "/tmp/foo.py"
    assert "intent_summary" in evt["payload"]
    assert "def foo" in evt["payload"]["intent_summary"]


def test_pre_tool_use_bash_omits_intent_summary(monkeypatch) -> None:
    """Bash 工具不应携带 intent_summary（隐私面，plan 显式约束）。"""
    captured = _capture_emit(monkeypatch)
    hooks_mod._hook_pre_tool_use(
        tool="claude-code",
        payload={
            "tool_name": "Bash",
            "command": "rm -rf /etc/secret",
            "session_id": "sess-2",
        },
        creds=None,
        runtime=_runtime(),
    )
    assert len(captured) == 1
    evt = captured[0]
    assert evt["kind"] == "pre_tool_use"
    assert evt["payload"]["tool"] == "Bash"
    assert "intent_summary" not in evt["payload"], (
        "Bash 不应携带 intent_summary（隐私）"
    )


def test_pre_tool_use_write_writes_intent_summary(monkeypatch) -> None:
    """Write 工具用 content 字段作为 intent_summary 源。"""
    captured = _capture_emit(monkeypatch)
    hooks_mod._hook_pre_tool_use(
        tool="claude-code",
        payload={
            "tool_name": "Write",
            "file_path": "/tmp/new.md",
            "content": "# Hello\n",
            "session_id": "sess-3",
        },
        creds=None,
        runtime=_runtime(),
    )
    evt = captured[0]
    assert evt["payload"]["tool"] == "Write"
    assert "intent_summary" in evt["payload"]
    assert "Hello" in evt["payload"]["intent_summary"]


def test_pre_tool_use_redacts_secrets_in_intent(monkeypatch) -> None:
    """new_string 中若含密钥应被脱敏。"""
    captured = _capture_emit(monkeypatch)
    hooks_mod._hook_pre_tool_use(
        tool="claude-code",
        payload={
            "tool_name": "Edit",
            "file_path": "/tmp/secret.py",
            "new_string": "API_KEY = 'sk-ABC1234567890DEF1234567890XYZ'",
        },
        creds=None,
        runtime=_runtime(),
    )
    intent = captured[0]["payload"]["intent_summary"]
    # 密钥不应原样出现
    assert "sk-ABC1234567890DEF1234567890XYZ" not in intent
