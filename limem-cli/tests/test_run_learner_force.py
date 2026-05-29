"""手动触发被动学习（force 模式）：忽略 batch_hash 去重、不推进游标。"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from limem.daemon import server as srv


def _runtime(**overrides):
    base = {
        "passive_learning_enabled": True,
        "passive_learning_idle_seconds": 180,
        "passive_learning_auto_submit": False,
        "passive_learning_min_events": 2,
        "learner_correction_window_hours": 48,
        "learner_jaccard_threshold": 0.4,
        "ngram_window_days": 14,
        "ngram_min_occurrences": 3,
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
        _passive_dirty=True,
        _last_processed_correction_ts=0,
        _last_processed_post_tool_ts=0,
        _active_passive_batch_hashes=set(),
        state=SimpleNamespace(suggestion_count=0, active_memories=0),
    )
    daemon._passive_batch_hash = srv.Daemon._passive_batch_hash.__get__(daemon)
    daemon._submit_passive_suggestions = srv.Daemon._submit_passive_suggestions.__get__(daemon)
    daemon._run_learner_once = srv.Daemon._run_learner_once.__get__(daemon)
    return daemon


def _two_similar_corrections():
    return [
        {
            "ts": 1000,
            "project_id": "proj/demo",
            "scope": "project:proj/demo",
            "prompt": "不要在 dev server 上运行构建命令",
            "session_id": "s1",
            "is_correction_confidence": 0.9,
        },
        {
            "ts": 1001,
            "project_id": "proj/demo",
            "scope": "project:proj/demo",
            "prompt": "别在 dev server 上运行构建命令了",
            "session_id": "s2",
            "is_correction_confidence": 0.9,
        },
    ]


def test_force_bypasses_batch_hash_and_keeps_cursors(monkeypatch) -> None:
    saved: list = []
    monkeypatch.setattr(srv, "load_suggestions", lambda: [])
    monkeypatch.setattr(srv, "save_suggestions", lambda items: saved.append(items))
    monkeypatch.setattr(srv, "archive_old", lambda items, **_kw: items)
    monkeypatch.setattr(srv.time, "time", lambda: 1100)

    daemon = _daemon()
    events = _two_similar_corrections()
    for e in events:
        daemon._correction_buf.append(e)

    # 预先把本批 batch_hash 标记为已处理：正常路径会因去重直接返回。
    batch_hash = daemon._passive_batch_hash(events, [])
    daemon._active_passive_batch_hashes.add(batch_hash)

    # 正常模式：因 batch_hash 命中 → 0 建议
    n_normal = asyncio.run(daemon._run_learner_once(force=False))
    assert n_normal == 0
    assert saved == []

    # force 模式：忽略去重 → 产出建议
    n_force = asyncio.run(daemon._run_learner_once(force=True))
    assert n_force >= 1
    assert saved and saved[-1]

    # force 不推进游标、不污染 batch_hash 锁集合（仍只有最初那一个）
    assert daemon._last_processed_correction_ts == 0
    assert daemon._last_processed_post_tool_ts == 0
    assert daemon._active_passive_batch_hashes == {batch_hash}


def test_force_returns_zero_when_no_events(monkeypatch) -> None:
    monkeypatch.setattr(srv, "load_suggestions", lambda: [])
    monkeypatch.setattr(srv, "save_suggestions", lambda items: None)
    monkeypatch.setattr(srv, "archive_old", lambda items, **_kw: items)
    monkeypatch.setattr(srv.time, "time", lambda: 1100)

    daemon = _daemon()
    assert asyncio.run(daemon._run_learner_once(force=True)) == 0
