from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from limem.daemon import server as srv


def _runtime(**overrides):
    base = {
        "is_correction_confidence_threshold": 0.5,
        "pre_post_pair_window_seconds": 60,
        "passive_learning_enabled": True,
        "passive_learning_idle_seconds": 180,
        "passive_learning_auto_submit": False,
        "passive_learning_min_events": 1,
        "learner_correction_window_hours": 24,
        "learner_jaccard_threshold": 0.4,
        "ngram_window_days": 7,
        "ngram_min_occurrences": 5,
        "ngram_min_accept_rate": 0.8,
        "suggestions_max_active": 500,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _daemon(**runtime_overrides):
    daemon = SimpleNamespace(
        runtime=_runtime(**runtime_overrides),
        _correction_buf=deque(maxlen=100),
        _post_tool_buf=deque(maxlen=100),
        _assistant_evidence_by_session={},
        _pending_intents={},
        _passive_dirty=False,
        _last_learnable_event_ts=0,
        _last_processed_correction_ts=0,
        _last_processed_post_tool_ts=0,
        _active_passive_batch_hashes=set(),
        _learner_wakeup=SimpleNamespace(set=lambda: None),
        state=SimpleNamespace(suggestion_count=0, active_memories=0),
    )
    daemon._consume_assistant_evidence = srv.Daemon._consume_assistant_evidence.__get__(daemon)
    daemon._remember_assistant_evidence = srv.Daemon._remember_assistant_evidence.__get__(daemon)
    daemon._mark_passive_dirty = srv.Daemon._mark_passive_dirty.__get__(daemon)
    daemon._passive_batch_hash = srv.Daemon._passive_batch_hash.__get__(daemon)
    daemon._submit_passive_suggestions = srv.Daemon._submit_passive_suggestions.__get__(daemon)
    daemon._run_learner_once = srv.Daemon._run_learner_once.__get__(daemon)
    return daemon


def test_user_correction_pairs_previous_assistant_evidence() -> None:
    daemon = _daemon()
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 100,
            "kind": "assistant_evidence",
            "tool": "codex",
            "session_id": "sess-a",
            "project_id": "p",
            "scope": "project:p",
            "payload": {"content": "I will send the full transcript to ingest."},
        },
    )
    srv.Daemon._handle_event_row(
        daemon,
        {
            "ts": 110,
            "kind": "user_prompt_submit",
            "tool": "codex",
            "session_id": "sess-a",
            "project_id": "p",
            "scope": "project:p",
            "payload": {"prompt": "不对，不要把完整对话送入后端。"},
        },
    )

    assert len(daemon._correction_buf) == 1
    assert daemon._correction_buf[0]["prev_assistant_head"] == (
        "I will send the full transcript to ingest."
    )
    assert daemon._assistant_evidence_by_session == {}
    assert daemon._passive_dirty is True


def test_idle_passive_learning_waits_until_stream_is_idle(monkeypatch) -> None:
    daemon = _daemon(passive_learning_idle_seconds=180)
    daemon._correction_buf.append(
        {
            "ts": 100,
            "project_id": "p",
            "scope": "project:p",
            "prompt": "不对，不要把完整对话送入后端。",
            "prev_assistant_head": "I will send the full transcript to ingest.",
            "is_correction_confidence": 0.9,
            "session_id": "sess-a",
            "tool": "codex",
            "evidence_id": "e1",
        }
    )
    daemon._passive_dirty = True
    daemon._last_learnable_event_ts = 100

    called = 0

    async def _run_once():
        nonlocal called
        called += 1

    daemon._run_learner_once = _run_once
    monkeypatch.setattr(srv.time, "time", lambda: 200)
    asyncio.run(srv.Daemon._run_learner_if_idle(daemon))
    assert called == 0

    monkeypatch.setattr(srv.time, "time", lambda: 281)
    asyncio.run(srv.Daemon._run_learner_if_idle(daemon))
    assert called == 1


def test_idle_passive_learning_processes_batch_once(monkeypatch, tmp_path) -> None:
    daemon = _daemon(passive_learning_idle_seconds=0, passive_learning_auto_submit=False)
    now = 1_900_000_000
    daemon._correction_buf.extend(
        [
            {
                "ts": now - 20,
                "project_id": "p",
                "scope": "project:p",
                "prompt": "不对，不要把完整对话送入后端。",
                "prev_assistant_head": "I will send the full transcript to ingest.",
                "is_correction_confidence": 0.9,
                "session_id": "sess-a",
                "tool": "codex",
                "evidence_id": "e1",
            },
            {
                "ts": now - 10,
                "project_id": "p",
                "scope": "project:p",
                "prompt": "不对，不要把完整对话送入后端。",
                "prev_assistant_head": "I will send the full transcript to ingest.",
                "is_correction_confidence": 0.9,
                "session_id": "sess-b",
                "tool": "codex",
                "evidence_id": "e2",
            },
        ]
    )
    daemon._passive_dirty = True
    daemon._last_learnable_event_ts = now - 10

    saved: list[list[dict]] = []
    monkeypatch.setattr(srv, "load_suggestions", lambda: [])
    monkeypatch.setattr(srv, "save_suggestions", lambda items: saved.append(items))
    monkeypatch.setattr(srv, "archive_old", lambda items, **_kw: items)

    monkeypatch.setattr(srv.time, "time", lambda: now)
    asyncio.run(srv.Daemon._run_learner_once(daemon))
    assert daemon._passive_dirty is False
    assert daemon._last_processed_correction_ts == now - 10
    assert len(saved) == 1
    assert saved[0][0]["metadata"]["passive_batch_hash"].startswith("plb_")

    daemon._passive_dirty = True
    asyncio.run(srv.Daemon._run_learner_once(daemon))
    assert len(saved) == 1
