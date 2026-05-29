"""Codex notify 包装程序：摘要、记录挑选、链式转发、永不抛/非零退出。"""

from __future__ import annotations

import json

from limem import notify as nmod


def test_summarize_recall_record_basic() -> None:
    record = {
        "items": [
            {"src": "hard", "short_id": "abcd0001ffff", "summary_head": "部署前跑测试"},
            {"src": "pattern", "canonical": "project", "heading": "## 规范"},
            {"src": "bm25", "summary_head": "docker 构建说明"},
            {"src": "hard", "summary_head": "第四条"},
        ]
    }
    out = nmod.summarize_recall_record(record)
    assert out is not None
    title, body = out
    assert "本次引用 4 条记忆" in title
    assert "规则 #abcd0001" in body
    assert "档案" in body
    assert "…另 1 条" in body  # 只展示前 3 条 + 余量


def test_summarize_recall_record_empty() -> None:
    assert nmod.summarize_recall_record(None) is None
    assert nmod.summarize_recall_record({"items": []}) is None


def test_pick_recall_record_prefers_thread_id() -> None:
    records = [
        {"session_id": "other", "items": [{"src": "hard"}]},
        {"session_id": "tid-9", "items": [{"src": "bm25"}]},
    ]
    rec = nmod.pick_recall_record(records, thread_id="tid-9")
    assert rec["session_id"] == "tid-9"
    # 无匹配 → 取最新（列表头）
    rec2 = nmod.pick_recall_record(records, thread_id="missing")
    assert rec2["session_id"] == "other"
    assert nmod.pick_recall_record([], thread_id="x") is None


def test_notify_codex_main_invokes_os_notify(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(nmod, "os_notify", lambda t, b: calls.append((t, b)) or True)
    monkeypatch.setattr(
        nmod, "_forward_prev_notify", lambda _arg: None
    )
    monkeypatch.setattr(
        "limem.daemon_client.list_recent_recalls",
        lambda limit=10: [
            {"session_id": "tid-1", "items": [{"src": "hard", "summary_head": "x"}]}
        ],
    )
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "tid-1"})
    rc = nmod.notify_codex_main([payload])
    assert rc == 0
    assert calls and "本次引用 1 条记忆" in calls[0][0]


def test_notify_codex_main_ignores_non_turn_complete(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(nmod, "os_notify", lambda t, b: calls.append((t, b)))
    monkeypatch.setattr(nmod, "_forward_prev_notify", lambda _arg: None)
    rc = nmod.notify_codex_main([json.dumps({"type": "other"})])
    assert rc == 0
    assert calls == []


def test_notify_codex_main_bad_payload_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(nmod, "_forward_prev_notify", lambda _arg: None)
    assert nmod.notify_codex_main(["not json {"]) == 0
    assert nmod.notify_codex_main([]) == 0


def test_forward_prev_notify_chains_user_program(monkeypatch, tmp_path) -> None:
    sidecar = tmp_path / "prev.json"
    sidecar.write_text(json.dumps(["/usr/bin/true", "--flag"]))
    monkeypatch.setattr(nmod, "CODEX_PREV_NOTIFY_PATH", sidecar)
    popen_calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, args, **_kw):
            popen_calls.append(args)

    monkeypatch.setattr(nmod.subprocess, "Popen", _FakePopen)
    nmod._forward_prev_notify("PAYLOAD_JSON")
    assert popen_calls == [["/usr/bin/true", "--flag", "PAYLOAD_JSON"]]


def test_forward_prev_notify_no_sidecar_is_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(nmod, "CODEX_PREV_NOTIFY_PATH", tmp_path / "missing.json")
    called = []
    monkeypatch.setattr(nmod.subprocess, "Popen", lambda *a, **k: called.append(a))
    nmod._forward_prev_notify("X")
    assert called == []
