"""A1.2：PreToolUse 与 PostToolUse 在 daemon 内存中按 (session_id, file_path) 配对。

仅 Edit/Write/NotebookEdit 进配对池（A1.1 hook 端约束）；配对结果挂在 _post_tool_buf
末项的 intent_summary / pair_age_seconds 字段，不写 events.ndjson。
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from limem.daemon import server as srv


def _build_daemon() -> SimpleNamespace:
    runtime = SimpleNamespace(
        is_correction_confidence_threshold=0.5,
        pre_post_pair_window_seconds=60,
    )
    daemon = SimpleNamespace(
        runtime=runtime,
        _correction_buf=deque(maxlen=100),
        _post_tool_buf=deque(maxlen=100),
        _pending_intents={},
        _learner_wakeup=SimpleNamespace(set=lambda: None),
        _buf_max=100,
    )
    daemon._evict_old_intents = lambda now: srv.Daemon._evict_old_intents(daemon, now=now)
    return daemon


def test_pre_post_pairing_attaches_intent_summary() -> None:
    daemon = _build_daemon()
    pre_row = {
        "ts": 1000,
        "kind": "pre_tool_use",
        "tool": "claude-code",
        "session_id": "sess-A",
        "project_id": "p",
        "scope": "project:p",
        "payload": {
            "tool": "Edit",
            "file_path": "/tmp/x.py",
            "intent_summary": "def foo(): return 1",
        },
    }
    srv.Daemon._handle_event_row(daemon, pre_row)
    assert ("sess-A", "/tmp/x.py") in daemon._pending_intents

    post_row = {
        "ts": 1010,
        "kind": "post_tool_use",
        "tool": "claude-code",
        "session_id": "sess-A",
        "project_id": "p",
        "scope": "project:p",
        "payload": {
            "tool": "Edit",
            "file_path": "/tmp/x.py",
            "accepted": True,
            "diff_summary": "old:  | new: def foo(): return 1",
        },
    }
    srv.Daemon._handle_event_row(daemon, post_row)

    # 配对应消费 _pending_intents
    assert ("sess-A", "/tmp/x.py") not in daemon._pending_intents
    last = daemon._post_tool_buf[-1]
    assert last["intent_summary"] == "def foo(): return 1"
    assert last["pair_age_seconds"] == 10


def test_pre_post_pairing_window_expiry() -> None:
    """超过 pre_post_pair_window_seconds（60s）的 pre 不再与 post 配对。"""
    daemon = _build_daemon()
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 1000,
            "kind": "pre_tool_use",
            "tool": "claude-code",
            "session_id": "sess-B",
            "project_id": "p",
            "scope": "project:p",
            "payload": {
                "tool": "Edit",
                "file_path": "/tmp/y.py",
                "intent_summary": "stale",
            },
        },
    )
    # post 在 100s 后到来 → 超出窗口
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 1100,
            "kind": "post_tool_use",
            "tool": "claude-code",
            "session_id": "sess-B",
            "project_id": "p",
            "scope": "project:p",
            "payload": {
                "tool": "Edit",
                "file_path": "/tmp/y.py",
                "accepted": True,
                "diff_summary": "diff",
            },
        },
    )
    last = daemon._post_tool_buf[-1]
    assert "intent_summary" not in last
    assert "pair_age_seconds" not in last


def test_bash_not_in_pairing_pool() -> None:
    """Bash 工具不参与配对（隐私面 + plan 显式约束）。"""
    daemon = _build_daemon()
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 2000,
            "kind": "pre_tool_use",
            "tool": "claude-code",
            "session_id": "sess-C",
            "project_id": "p",
            "scope": "project:p",
            "payload": {"tool": "Bash", "file_path": ""},
        },
    )
    assert ("sess-C", "") not in daemon._pending_intents
    assert not daemon._pending_intents
