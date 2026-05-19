"""A3：session_end 行触发 _learner_wakeup.set()；不做 session-only flush。"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from limem.daemon import server as srv


def test_session_end_sets_learner_wakeup() -> None:
    wakeup = asyncio.Event()
    runtime = SimpleNamespace(
        is_correction_confidence_threshold=0.5,
        pre_post_pair_window_seconds=60,
    )
    daemon = SimpleNamespace(
        runtime=runtime,
        _correction_buf=deque(maxlen=100),
        _post_tool_buf=deque(maxlen=100),
        _pending_intents={},
        _learner_wakeup=wakeup,
        _buf_max=100,
        _evict_old_intents=lambda now: None,
    )
    assert not wakeup.is_set()
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 5000,
            "kind": "session_end",
            "tool": "claude-code",
            "session_id": "sess-X",
            "project_id": "p",
            "scope": "project:p",
            "payload": {},
        },
    )
    assert wakeup.is_set(), "session_end 应触发 _learner_wakeup.set()"


def test_session_end_does_not_touch_buffers() -> None:
    """session_end 不应清理 buffer 中该 session_id 的事件（保 24h/7d 跨会话窗口）。"""
    wakeup = asyncio.Event()
    runtime = SimpleNamespace(
        is_correction_confidence_threshold=0.5,
        pre_post_pair_window_seconds=60,
    )
    pre_existing = {
        "ts": 4000,
        "project_id": "p",
        "scope": "project:p",
        "prompt": "不对",
        "session_id": "sess-X",
        "tool": "claude-code",
        "evidence_id": "e1",
    }
    daemon = SimpleNamespace(
        runtime=runtime,
        _correction_buf=deque([pre_existing], maxlen=100),
        _post_tool_buf=deque(maxlen=100),
        _pending_intents={},
        _learner_wakeup=wakeup,
        _buf_max=100,
        _evict_old_intents=lambda now: None,
    )
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 5000,
            "kind": "session_end",
            "tool": "claude-code",
            "session_id": "sess-X",
            "project_id": "p",
            "scope": "project:p",
            "payload": {},
        },
    )
    # buffer 内容不变（窗口证据保留给跨 session 24h 聚合）
    assert len(daemon._correction_buf) == 1
    assert daemon._correction_buf[0]["session_id"] == "sess-X"
