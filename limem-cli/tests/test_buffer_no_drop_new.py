"""D1：deque(maxlen=...) 满后必须丢最旧而非丢最新（修复 server.py 旧版 `len < _buf_max` bug）。"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from limem.daemon import server as srv


def _build_runtime(stub_threshold: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(
        is_correction_confidence_threshold=stub_threshold,
        pre_post_pair_window_seconds=60,
    )


def _make_daemon(
    *, maxlen: int = 3
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=_build_runtime(),
        _correction_buf=deque(maxlen=maxlen),
        _post_tool_buf=deque(maxlen=maxlen),
        _pending_intents={},
        _learner_wakeup=SimpleNamespace(set=lambda: None),
        _buf_max=maxlen,
        _evict_old_intents=lambda now: None,
    )


def test_correction_buffer_drops_oldest_not_newest() -> None:
    daemon = _make_daemon(maxlen=3)
    for i in range(5):
        row = {
            "ts": 100 + i,
            "kind": "user_prompt_submit",
            "tool": "claude-code",
            "session_id": f"s{i}",
            "project_id": "p",
            "scope": "project:p",
            # 关键词必须命中 _CN_CORRECT 否则 score_correction 返回 False
            "payload": {"prompt": f"不对，应该改成 v{i}"},
        }
        srv.Daemon._handle_event_row(daemon, row)
    # maxlen=3：5 条进、最旧 2 条被丢、最新 3 条（ts=102,103,104）保留
    assert len(daemon._correction_buf) == 3
    ts_seq = [item["ts"] for item in daemon._correction_buf]
    assert ts_seq == [102, 103, 104], (
        f"buffer 留下最新 3 条，实际 {ts_seq}（说明又出现了'满了丢新事件'回归）"
    )


def test_post_tool_buffer_drops_oldest_not_newest() -> None:
    daemon = _make_daemon(maxlen=3)
    for i in range(5):
        row = {
            "ts": 200 + i,
            "kind": "post_tool_use",
            "tool": "claude-code",
            "session_id": f"s{i}",
            "project_id": "p",
            "scope": "project:p",
            "payload": {
                "tool": "Edit",
                "file_path": f"/tmp/f{i}.py",
                "accepted": True,
                "diff_summary": f"diff-{i}",
            },
        }
        srv.Daemon._handle_event_row(daemon, row)
    assert len(daemon._post_tool_buf) == 3
    ts_seq = [item["ts"] for item in daemon._post_tool_buf]
    assert ts_seq == [202, 203, 204]
