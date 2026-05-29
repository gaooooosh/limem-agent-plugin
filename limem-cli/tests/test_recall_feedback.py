"""记忆使用反馈链路：注入产物 → daemon 上报 → statusline / dash 消费。

测试覆盖：
- injector: 双签名 / rendered_items 与 short_id 对齐
- hooks: render 后 fire-and-forget 上报；daemon 不可达不阻塞
- daemon: report_recall / list_recent_recalls / 防回放 / 落盘
- statusline: ✨ 摘要拼接 / 向后兼容
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from limem.injector import (
    Budgets,
    InjectItem,
    render_backend_recall,
    render_inject,
    render_inject_with_diagnostics,
)

# ---------- injector ----------


def _hard_item(*, event_id: str, short_id: str, summary: str, score: float) -> InjectItem:
    return InjectItem(
        kind="hard",
        score=score,
        event_id=event_id,
        mem_type="rule",
        scope="global",
        summary=summary,
        importance=0.9,
        ts=1700000000,
        short_id=short_id,
    )


def test_render_inject_with_diagnostics_returns_rendered_items() -> None:
    items = [
        _hard_item(event_id="evt_a", short_id="aaaaaaaaaaaa", summary="A 规则", score=0.9),
        _hard_item(event_id="evt_b", short_id="bbbbbbbbbbbb", summary="B 规则", score=0.8),
    ]
    text, rendered = render_inject_with_diagnostics(
        items,
        budgets=Budgets(hard=400, pattern=0, soft=0),
    )
    assert text != ""
    assert len(rendered) == 2
    # recall="N" 的 N == rendered 数
    assert 'recall="2"' in text


def test_render_inject_drops_items_when_budget_too_small() -> None:
    """budget 只够 1 条时第二条被丢；rendered 与 text 中的 recall 数严格一致。"""
    items = [
        _hard_item(event_id="evt_a", short_id="aaaaaaaaaaaa", summary="A" * 100, score=0.9),
        _hard_item(event_id="evt_b", short_id="bbbbbbbbbbbb", summary="B" * 100, score=0.8),
    ]
    # 每条 render_line 约 ~70-110 字（含 metadata），预算 200 大约只够 1 条
    text, rendered = render_inject_with_diagnostics(
        items, budgets=Budgets(hard=200, pattern=0, soft=0), per_item_chars=120,
    )
    # 第一条至少进得去，第二条被丢
    assert len(rendered) == 1
    assert rendered[0].event_id == "evt_a"
    assert 'recall="1"' in text
    assert "#aaaaaaaaaaaa" in text
    assert "#bbbbbbbbbbbb" not in text


def test_render_inject_diagnostics_short_id_alignment() -> None:
    items = [
        _hard_item(event_id="evt_a", short_id="aaaaaaaaaaaa", summary="X", score=0.9),
        _hard_item(event_id="evt_b", short_id="bbbbbbbbbbbb", summary="Y", score=0.8),
    ]
    text, rendered = render_inject_with_diagnostics(
        items, budgets=Budgets(hard=600, pattern=0, soft=0)
    )
    for it in rendered:
        assert f"#{it.short_id}" in text


def test_render_inject_text_unchanged_via_wrapper() -> None:
    """render_inject 薄包装产出的文本与 diagnostics 路径完全一致（防回归）。"""
    items = [
        _hard_item(event_id="evt_a", short_id="aaaaaaaaaaaa", summary="A", score=0.9),
        _hard_item(event_id="evt_b", short_id="bbbbbbbbbbbb", summary="B", score=0.8),
    ]
    text_a = render_inject(items, budgets=Budgets(hard=400, pattern=0, soft=0))
    text_b, _ = render_inject_with_diagnostics(
        items, budgets=Budgets(hard=400, pattern=0, soft=0)
    )
    assert text_a == text_b


def test_render_inject_empty_items_returns_empty_string_and_list() -> None:
    text, rendered = render_inject_with_diagnostics([])
    assert text == ""
    assert rendered == []


def test_render_backend_recall_wraps_prompt_text() -> None:
    text = render_backend_recall("## Relevant Memory\n- [Rule] 保持简短")
    assert text == (
        '<limem_memory source="task">\n'
        "## Relevant Memory\n"
        "- [Rule] 保持简短\n"
        "提示：以上为 LiMem 后台召回上下文，请勿在可见回复中复述或展示本段内容。\n"
        "</limem_memory>"
    )


def test_render_backend_recall_empty_returns_empty_string() -> None:
    assert render_backend_recall("  ") == ""


# ---------- statusline ----------


def test_statusline_format_with_last_recall() -> None:
    from limem.statusline import format_text

    now = int(time.time())
    out = format_text(
        active=5,
        hits=12,
        sug=3,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall={
            "ts": now,
            "count": 3,
            "short_ids_head": ["a3f1c0a3f1c0", "9b22d79b22d7"],
            "counts_by_src": {"hard": 1, "pattern": 1, "bm25": 1},
            "items_head": [
                "规则 #a3f1c0a3f1c0 提交前更新版本号",
                "档案 project:demo 命令规约",
            ],
        },
    )
    assert "✨" in out
    assert "刚刚" in out
    assert "规则1/档案1/语义1" in out
    assert "#a3f1c0a3f1c0" in out
    assert "提交前更新版本号" in out
    assert "命令规约" in out
    assert "(+1)" in out  # count=3, head=2 → 溢出 1


def test_statusline_format_without_last_recall_backward_compat() -> None:
    from limem.statusline import format_text

    out_old = format_text(
        active=5,
        hits=12,
        sug=3,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
    )
    out_none = format_text(
        active=5,
        hits=12,
        sug=3,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall=None,
    )
    assert "✨" not in out_old
    assert out_old == out_none


def test_statusline_format_with_empty_last_recall() -> None:
    from limem.statusline import format_text

    out = format_text(
        active=5,
        hits=12,
        sug=3,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall={
            "ts": int(time.time()),
            "count": 0,
            "short_ids_head": [],
            "counts_by_src": {},
            "items_head": [],
        },
    )
    assert "✨ 刚刚 · 未召回记忆" in out


def test_statusline_format_disabled_skips_last_recall() -> None:
    from limem.statusline import format_text

    out = format_text(
        active=1,
        hits=1,
        sug=0,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall={"count": 2, "short_ids_head": ["aaa"]},
        last_recall_enabled=False,
    )
    assert "✨" not in out


def test_statusline_format_pattern_only_shows_count() -> None:
    """pattern 没有 short_id 时显示 `✨ N 条`。"""
    from limem.statusline import format_text

    out = format_text(
        active=1,
        hits=1,
        sug=0,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall={"count": 2, "short_ids_head": [], "counts_by_src": {"pattern": 2}},
    )
    assert "✨ 档案2 · 2 条" in out


def test_statusline_format_legacy_last_recall_still_shows_short_ids() -> None:
    from limem.statusline import format_text

    out = format_text(
        active=1,
        hits=1,
        sug=0,
        pause_on=False,
        pause_until_ts=None,
        connectivity="healthy",
        reason=None,
        init_pending_until_ts=None,
        inited_now_ts=None,
        last_recall={
            "count": 2,
            "short_ids_head": ["aaa111", "bbb222"],
            "counts_by_src": {"hard": 1, "bm25": 1},
        },
    )
    assert "#aaa111" in out
    assert "#bbb222" in out
    assert "规则1/语义1" in out


# ---------- daemon state ----------


def _make_state(tmp_cache_dir, monkeypatch):
    """构造 DaemonState 实例，并把 RECENT_RECALLS_PATH 指向 tmp。"""
    new_path = tmp_cache_dir / "recent_recalls.json"
    monkeypatch.setattr(
        "limem.daemon.state.RECENT_RECALLS_PATH", new_path, raising=True
    )
    from limem.daemon.state import DaemonState

    st = DaemonState()
    st.set_recent_recalls_max(20)
    return st, new_path


def _make_record(
    *,
    ts: int,
    items: list[dict[str, Any]] | None = None,
    scope: str = "global",
    prompt_head: str | None = None,
) -> Any:
    from limem.daemon.state import RecalledItem, RecallEmittedRecord

    return RecallEmittedRecord(
        ts=ts,
        session_id=f"sess-{ts}",
        project_id="proj",
        scope=scope,
        items=[RecalledItem(**it) for it in (items or [])],
        via_patterns=["proj"],
        via_keywords=["docker"],
        prompt_head=prompt_head if prompt_head is not None else f"prompt {ts}",
        injected_chars=100,
    )


def test_daemon_state_record_recall_updates_last_recall(tmp_path, monkeypatch) -> None:
    st, _ = _make_state(tmp_path, monkeypatch)
    rec = _make_record(
        ts=1700000000,
        items=[
            {"short_id": "aaa111aaa111", "event_id": "e_a", "src": "hard", "mem_type": "rule",
             "scope": "global", "summary_head": "rule body"},
            {"short_id": "bbb222bbb222", "event_id": "e_b", "src": "bm25", "mem_type": "note",
             "scope": "global", "summary_head": "note body"},
            {"short_id": "", "event_id": "", "src": "pattern", "mem_type": "",
             "scope": "global", "summary_head": "pattern slice", "canonical": "project:proj",
             "heading": "命令规约"},
        ],
    )
    st.record_recall(rec)
    assert len(st.recent_recalls) == 1
    assert st.last_recall is not None
    assert st.last_recall.count == 3
    assert st.last_recall.counts_by_src == {"hard": 1, "bm25": 1, "pattern": 1}
    # short_ids 只取有 short_id 的两条，且最多 2 个
    assert st.last_recall.short_ids_head == ["aaa111aaa111", "bbb222bbb222"]
    assert st.last_recall.items_head == [
        "规则 #aaa111aaa111 rule body",
        "语义 #bbb222bbb222 note body",
    ]
    assert st.seen_recall_keys("sess-1700000000") == {
        "event:e_a",
        "event:e_b",
        "pattern:project:proj:命令规约",
    }


def test_daemon_state_record_empty_recall_updates_last_recall(tmp_path, monkeypatch) -> None:
    st, _ = _make_state(tmp_path, monkeypatch)
    rec = _make_record(ts=1700000000, items=[], prompt_head="没有匹配的请求")
    st.record_recall(rec)
    assert len(st.recent_recalls) == 1
    assert st.last_recall is not None
    assert st.last_recall.count == 0
    assert st.last_recall.short_ids_head == []
    assert st.last_recall.items_head == []
    assert st.consume_pending_recall("sess-1700000000") is rec


def test_daemon_state_recent_recalls_capped_at_max(tmp_path, monkeypatch) -> None:
    st, _ = _make_state(tmp_path, monkeypatch)
    st.set_recent_recalls_max(5)
    for i in range(10):
        st.record_recall(_make_record(ts=1700000000 + i))
    assert len(st.recent_recalls) == 5
    # newest first
    assert st.recent_recalls[0].ts == 1700000009
    assert st.recent_recalls[-1].ts == 1700000005


def test_daemon_state_persist_and_load(tmp_path, monkeypatch) -> None:
    st, path = _make_state(tmp_path, monkeypatch)
    st.record_recall(
        _make_record(
            ts=1700000123,
            items=[
                {"short_id": "ssss11112222", "event_id": "e1", "src": "hard",
                 "mem_type": "rule", "scope": "global", "summary_head": "x"}
            ],
        )
    )
    st.save_recent_recalls_to_disk()
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["records"][0]["items"][0]["short_id"] == "ssss11112222"

    # 新 state 从盘恢复
    from limem.daemon.state import DaemonState

    st2 = DaemonState()
    st2.set_recent_recalls_max(20)
    st2.load_recent_recalls_from_disk()
    assert len(st2.recent_recalls) == 1
    assert st2.recent_recalls[0].ts == 1700000123
    assert st2.last_recall is not None
    assert st2.last_recall.count == 1


# ---------- daemon RPC handlers ----------


class _FakeDaemon:
    """最小化 Daemon stub，只放必需字段，供 _h_report_recall / _h_list_recent_recalls 调用。"""

    def __init__(self, state, runtime) -> None:
        self.state = state
        self.runtime = runtime


def test_daemon_h_report_recall_updates_state_and_emits_event(
    tmp_path, monkeypatch
) -> None:
    # 隔离 RECENT_RECALLS_PATH 和 EVENTS_LOG_PATH
    rr_path = tmp_path / "recent_recalls.json"
    events_path = tmp_path / "hooks.log"
    monkeypatch.setattr("limem.daemon.state.RECENT_RECALLS_PATH", rr_path)
    monkeypatch.setattr("limem.daemon.eventbus.EVENTS_LOG_PATH", events_path)

    from limem.config import RuntimeConfig
    from limem.daemon.server import Daemon
    from limem.daemon.state import DaemonState

    # 跳过 Daemon.__init__ 内重资源（PatternIndex 等），手动构造
    fake = Daemon.__new__(Daemon)
    fake.state = DaemonState()
    fake.state.set_recent_recalls_max(20)
    fake.runtime = RuntimeConfig()

    payload = {
        "ts": 1700000000,
        "session_id": "sess-1",
        "project_id": "proj",
        "scope": "project:proj",
        "items": [
            {"short_id": "aaaa11112222", "event_id": "e1", "src": "hard",
             "mem_type": "rule", "scope": "project:proj",
             "summary_head": "禁止 npm run dev"},
            {"short_id": "bbbb33334444", "event_id": "e2", "src": "bm25",
             "mem_type": "note", "scope": "global", "summary_head": "另一条"},
        ],
        "via_patterns": ["project:proj"],
        "via_keywords": ["docker"],
        "prompt_head": "起一下 dev",
        "injected_chars": 1234,
    }
    result = asyncio.run(fake._h_report_recall(payload))
    assert result == {"ok": True}
    seen = asyncio.run(fake._h_seen_recall_keys({"session_id": "sess-1"}))
    assert seen == {"keys": ["event:e1", "event:e2"]}
    assert len(fake.state.recent_recalls) == 1
    assert fake.state.last_recall.count == 2
    assert fake.state.last_recall.short_ids_head == ["aaaa11112222", "bbbb33334444"]
    assert fake.state.last_recall.items_head == [
        "规则 #aaaa11112222 禁止 npm run dev",
        "语义 #bbbb33334444 另一条",
    ]

    # 审计行写到了 events.ndjson
    assert events_path.exists()
    lines = [ln for ln in events_path.read_text().splitlines() if ln.strip()]
    assert lines, "expected at least one audit line"
    row = json.loads(lines[-1])
    assert row["kind"] == "recall_emitted"
    assert row["payload"]["counts"] == {"hard": 1, "bm25": 1}
    assert row["payload"]["prompt_head"] == "起一下 dev"


def test_daemon_recall_emitted_not_replayed(tmp_path, monkeypatch) -> None:
    """_handle_event_row 收到 kind=='recall_emitted' 时直接 return，不污染 state。"""
    monkeypatch.setattr("limem.daemon.state.RECENT_RECALLS_PATH", tmp_path / "rr.json")
    monkeypatch.setattr("limem.daemon.eventbus.EVENTS_LOG_PATH", tmp_path / "ev.log")

    from limem.daemon.server import Daemon
    from limem.daemon.state import DaemonState

    fake = Daemon.__new__(Daemon)
    fake.state = DaemonState()
    fake.state.set_recent_recalls_max(20)
    fake.runtime = type("R", (), {})()  # 不需要

    before = len(fake.state.recent_recalls)
    fake._handle_event_row(
        {
            "ts": 1700000000,
            "kind": "recall_emitted",
            "tool": "",
            "session_id": "s",
            "project_id": "p",
            "scope": "global",
            "payload": {"items": [], "counts": {}, "prompt_head": ""},
            "redacted": False,
        }
    )
    after = len(fake.state.recent_recalls)
    assert before == after  # 没变化 → 没回放


def test_daemon_h_list_recent_recalls_newest_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("limem.daemon.state.RECENT_RECALLS_PATH", tmp_path / "rr.json")
    monkeypatch.setattr("limem.daemon.eventbus.EVENTS_LOG_PATH", tmp_path / "ev.log")
    from limem.config import RuntimeConfig
    from limem.daemon.server import Daemon
    from limem.daemon.state import DaemonState

    fake = Daemon.__new__(Daemon)
    fake.state = DaemonState()
    fake.state.set_recent_recalls_max(20)
    fake.runtime = RuntimeConfig()

    for ts in (1700000001, 1700000002, 1700000003):
        asyncio.run(
            fake._h_report_recall(
                {
                    "ts": ts,
                    "session_id": f"s-{ts}",
                    "project_id": "proj",
                    "scope": "global",
                    "items": [
                        {"short_id": f"sid_{ts:08x}", "event_id": f"e_{ts}",
                         "src": "hard", "mem_type": "rule", "scope": "global",
                         "summary_head": "x"}
                    ],
                    "prompt_head": str(ts),
                }
            )
        )
    out = asyncio.run(fake._h_list_recent_recalls({"limit": 10}))
    assert len(out) == 3
    assert [r["ts"] for r in out] == [1700000003, 1700000002, 1700000001]


def test_daemon_h_get_status_includes_last_recall(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("limem.daemon.state.RECENT_RECALLS_PATH", tmp_path / "rr.json")
    monkeypatch.setattr("limem.daemon.eventbus.EVENTS_LOG_PATH", tmp_path / "ev.log")
    from limem.config import RuntimeConfig
    from limem.daemon.server import Daemon
    from limem.daemon.state import DaemonState

    fake = Daemon.__new__(Daemon)
    fake.state = DaemonState()
    fake.state.set_recent_recalls_max(20)
    fake.runtime = RuntimeConfig()

    asyncio.run(
        fake._h_report_recall(
            {
                "ts": 1700000099,
                "session_id": "s",
                "project_id": "p",
                "scope": "global",
                "items": [
                    {"short_id": "ssss00001111", "event_id": "e", "src": "hard",
                     "mem_type": "rule", "scope": "global", "summary_head": ""}
                ],
                "prompt_head": "",
            }
        )
    )
    status = asyncio.run(fake._h_get_status({}))
    assert "last_recall" in status
    lr = status["last_recall"]
    assert lr is not None
    assert lr["count"] == 1
    assert lr["short_ids_head"] == ["ssss00001111"]
    assert lr["items_head"] == ["规则 #ssss00001111"]


# ---------- hook 上报 ----------


def test_hook_report_recall_safe_calls_daemon(monkeypatch) -> None:
    """_report_recall_safe 走 daemon_client.report_recall；items 为空时不调。"""
    from limem import hooks as hmod

    calls: list[dict[str, Any]] = []

    def _capture(params):
        calls.append(params)

    monkeypatch.setattr(hmod.daemon_client, "report_recall", _capture)

    # 空 items：不上报
    hmod._report_recall_safe(
        rendered=[],
        session_id="s",
        project_id="p",
        scope="global",
        prompt="x",
        via_patterns=[],
        via_keywords=[],
        injected_chars=0,
    )
    assert calls == []

    # 有 items：上报一次，items src 映射 soft → bm25
    items = [
        InjectItem(
            kind="hard", score=1.0, event_id="e1", mem_type="rule",
            scope="global", summary="hello", short_id="aaaa11112222",
        ),
        InjectItem(
            kind="soft", score=0.5, event_id="e2", mem_type="note",
            scope="global", summary="world", short_id="bbbb33334444",
        ),
        InjectItem(
            kind="pattern", score=0.9, entity_id="p_proj",
            canonical="project:foo", heading="规约", pattern_content="内容",
        ),
    ]
    hmod._report_recall_safe(
        rendered=items,
        session_id="s",
        project_id="p",
        scope="global",
        prompt="起一下 dev",
        via_patterns=["project:foo"],
        via_keywords=["docker"],
        injected_chars=120,
    )
    assert len(calls) == 1
    p = calls[0]
    assert p["session_id"] == "s"
    assert p["prompt_head"] == "起一下 dev"
    srcs = [it["src"] for it in p["items"]]
    assert srcs == ["hard", "bm25", "pattern"]  # soft → bm25
    # pattern 条的 summary_head 取 pattern_content
    assert p["items"][2]["summary_head"] == "内容"
    assert p["items"][2]["canonical"] == "project:foo"
    assert p["items"][2]["heading"] == "规约"


def test_hook_report_recall_payload_allows_empty_when_requested(monkeypatch) -> None:
    from limem import hooks as hmod

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(hmod.daemon_client, "report_recall", lambda p: calls.append(p))

    hmod._report_recall_payload_safe(
        items_payload=[],
        session_id="s",
        project_id="p",
        scope="global",
        prompt="没有匹配",
        via_patterns=["project:p"],
        via_keywords=["没有"],
        injected_chars=0,
        allow_empty=True,
    )
    assert len(calls) == 1
    assert calls[0]["items"] == []
    assert calls[0]["prompt_head"] == "没有匹配"


def test_hook_report_recall_safe_swallows_daemon_exception(monkeypatch) -> None:
    from limem import hooks as hmod

    def _boom(params):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(hmod.daemon_client, "report_recall", _boom)

    items = [
        InjectItem(
            kind="hard", score=1.0, event_id="e1", mem_type="rule",
            scope="global", summary="x", short_id="aaaa11112222",
        )
    ]
    # 不抛
    hmod._report_recall_safe(
        rendered=items,
        session_id="s",
        project_id="p",
        scope="global",
        prompt="x",
        via_patterns=[],
        via_keywords=[],
        injected_chars=0,
    )


def test_hook_filter_seen_recall_items_keeps_hard_but_drops_seen_patterns(monkeypatch) -> None:
    """Hard rules remain citable each turn; session de-dupe only drops repeat context."""
    from limem import hooks as hmod

    monkeypatch.setattr(
        hmod.daemon_client,
        "seen_recall_keys",
        lambda session_id: {"event:e_seen", "pattern:project:demo:部署"},
    )

    items = [
        InjectItem(kind="hard", score=1.0, event_id="e_seen", summary="old"),
        InjectItem(
            kind="pattern",
            score=1.0,
            canonical="project:demo",
            heading="部署",
            pattern_content="old pattern",
        ),
        InjectItem(kind="hard", score=1.0, event_id="e_new", summary="new"),
    ]

    out = hmod._filter_seen_recall_items(items, session_id="sess-1")
    assert [it.event_id for it in out] == ["e_seen", "e_new"]
    assert all(it.kind == "hard" for it in out)


def test_hook_filter_seen_task_recall_drops_same_backend_text(monkeypatch) -> None:
    from limem import hooks as hmod

    text = "## Relevant Memory\n- [Context] 已经注入过的后端任务召回"
    key = hmod._task_recall_key(text)
    monkeypatch.setattr(
        hmod.daemon_client,
        "seen_recall_keys",
        lambda session_id: {key},
    )

    assert hmod._filter_seen_task_recall(text, session_id="sess-1") == ""


def test_hook_report_backend_recall_safe_records_task_source(monkeypatch) -> None:
    from limem import hooks as hmod

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(hmod.daemon_client, "report_recall", lambda p: calls.append(p))

    hmod._report_backend_recall_safe(
        task_text="## Relevant Memory\n- [Context] 自动任务召回",
        session_id="sess-1",
        project_id="proj",
        scope="project:proj",
        prompt="修复问题",
        via_keywords=["修复"],
        injected_chars=123,
    )

    assert len(calls) == 1
    item = calls[0]["items"][0]
    assert item["src"] == "task"
    assert item["mem_type"] == "task_recall"
    assert item["summary_head"].startswith("## Relevant Memory")


def test_mcp_recent_recalls_calls_daemon_first(monkeypatch) -> None:
    """daemon 命中时使用 daemon 数据，source 标记为 daemon。"""
    from limem import mcp_server

    sample = [
        {"ts": 1700000005, "scope": "global", "prompt_head": "p1",
         "items": [{"short_id": "abc", "src": "hard"}]},
        {"ts": 1700000003, "scope": "project:foo", "prompt_head": "p2",
         "items": [{"short_id": "def", "src": "bm25"}]},
    ]
    monkeypatch.setattr(
        mcp_server.daemon_client,
        "list_recent_recalls",
        lambda limit=20: list(sample),
    )
    out = mcp_server._t_recent_recalls(limit=5)
    data = json.loads(out)
    assert data["source"] == "daemon"
    assert data["count"] == 2
    assert data["records"][0]["prompt_head"] == "p1"


def test_mcp_recent_recalls_fallback_to_cache(monkeypatch, tmp_path) -> None:
    """daemon 不可达时读 RECENT_RECALLS_PATH，source 标记为 cache。"""
    from limem import mcp_server

    rr_path = tmp_path / "recent_recalls.json"
    rr_path.write_text(
        json.dumps(
            {
                "updated_ts": 1700000000,
                "records": [
                    {"ts": 1700000000, "scope": "global", "prompt_head": "cached",
                     "items": []}
                ],
            }
        )
    )
    monkeypatch.setattr(mcp_server, "RECENT_RECALLS_PATH", rr_path)
    monkeypatch.setattr(
        mcp_server.daemon_client, "list_recent_recalls", lambda limit=20: None
    )
    out = mcp_server._t_recent_recalls(limit=5)
    data = json.loads(out)
    assert data["source"] == "cache"
    assert data["count"] == 1
    assert data["records"][0]["prompt_head"] == "cached"


def test_mcp_recent_recalls_current_project_only_filter(monkeypatch) -> None:
    from limem import mcp_server

    sample = [
        {"ts": 1700000005, "scope": "project:foo", "items": []},
        {"ts": 1700000004, "scope": "global", "items": []},
        {"ts": 1700000003, "scope": "project:other", "items": []},
    ]
    monkeypatch.setattr(
        mcp_server.daemon_client, "list_recent_recalls", lambda limit=20: list(sample)
    )
    monkeypatch.setattr(mcp_server, "detect_project_id", lambda: "foo")

    out = mcp_server._t_recent_recalls(limit=10, current_project_only=True)
    data = json.loads(out)
    scopes = {r["scope"] for r in data["records"]}
    assert scopes == {"project:foo", "global"}  # other project filtered out


def test_daemon_consume_pending_recall_one_shot(tmp_path, monkeypatch) -> None:
    """consume 取出后 daemon 清除 pending；二次调用返回 None。"""
    st, _ = _make_state(tmp_path, monkeypatch)
    rec = _make_record(
        ts=1700000001,
        items=[
            {"short_id": "aaaa11112222", "event_id": "e1", "src": "hard",
             "mem_type": "rule", "scope": "global", "summary_head": "x"},
        ],
    )
    rec.session_id = "sess-A"
    st.record_recall(rec)
    out1 = st.consume_pending_recall("sess-A")
    assert out1 is not None
    assert out1.items[0].short_id == "aaaa11112222"
    # 第二次：已消费，pending 已清
    out2 = st.consume_pending_recall("sess-A")
    assert out2 is None


def test_daemon_consume_pending_recall_dedupe(tmp_path, monkeypatch) -> None:
    """两轮注入相同 short_id 集合时，第二轮 consume 返回 None（去重）。"""
    st, _ = _make_state(tmp_path, monkeypatch)
    same_items = [
        {"short_id": "aaaa11112222", "event_id": "e1", "src": "hard",
         "mem_type": "rule", "scope": "global", "summary_head": "x"},
    ]
    r1 = _make_record(ts=1700000001, items=same_items)
    r1.session_id = "sess-B"
    st.record_recall(r1)
    assert st.consume_pending_recall("sess-B") is not None

    r2 = _make_record(ts=1700000002, items=same_items)
    r2.session_id = "sess-B"
    st.record_recall(r2)
    # 签名相同 → 去重，不再展示
    assert st.consume_pending_recall("sess-B") is None


def test_daemon_consume_empty_recall_dedupe_uses_prompt_head(tmp_path, monkeypatch) -> None:
    st, _ = _make_state(tmp_path, monkeypatch)
    r1 = _make_record(ts=1700000001, items=[], prompt_head="请求 A")
    r1.session_id = "sess-B"
    st.record_recall(r1)
    assert st.consume_pending_recall("sess-B") is not None

    r2 = _make_record(ts=1700000002, items=[], prompt_head="请求 A")
    r2.session_id = "sess-B"
    st.record_recall(r2)
    assert st.consume_pending_recall("sess-B") is None

    r3 = _make_record(ts=1700000003, items=[], prompt_head="请求 B")
    r3.session_id = "sess-B"
    st.record_recall(r3)
    assert st.consume_pending_recall("sess-B") is not None


def test_daemon_consume_pending_recall_different_session_independent(
    tmp_path, monkeypatch
) -> None:
    """不同 session 的 pending 独立维护。"""
    st, _ = _make_state(tmp_path, monkeypatch)
    r_a = _make_record(
        ts=1700000001,
        items=[
            {"short_id": "aaaa11112222", "event_id": "e1", "src": "hard",
             "mem_type": "rule", "scope": "global", "summary_head": "x"},
        ],
    )
    r_a.session_id = "sess-A"
    st.record_recall(r_a)

    r_b = _make_record(
        ts=1700000002,
        items=[
            {"short_id": "bbbb33334444", "event_id": "e2", "src": "hard",
             "mem_type": "rule", "scope": "global", "summary_head": "y"},
        ],
    )
    r_b.session_id = "sess-B"
    st.record_recall(r_b)

    out_a = st.consume_pending_recall("sess-A")
    out_b = st.consume_pending_recall("sess-B")
    assert out_a is not None and out_a.items[0].short_id == "aaaa11112222"
    assert out_b is not None and out_b.items[0].short_id == "bbbb33334444"


def test_daemon_h_consume_pending_recall_via_rpc(tmp_path, monkeypatch) -> None:
    """同时验证 daemon RPC handler 行为。"""
    monkeypatch.setattr("limem.daemon.state.RECENT_RECALLS_PATH", tmp_path / "rr.json")
    monkeypatch.setattr("limem.daemon.eventbus.EVENTS_LOG_PATH", tmp_path / "ev.log")
    from limem.config import RuntimeConfig
    from limem.daemon.server import Daemon
    from limem.daemon.state import DaemonState

    fake = Daemon.__new__(Daemon)
    fake.state = DaemonState()
    fake.state.set_recent_recalls_max(20)
    fake.runtime = RuntimeConfig()
    asyncio.run(
        fake._h_report_recall(
            {
                "ts": 1700000099,
                "session_id": "sess-C",
                "project_id": "p",
                "scope": "global",
                "items": [
                    {"short_id": "cccc55556666", "event_id": "e3", "src": "hard",
                     "mem_type": "rule", "scope": "global", "summary_head": "z"},
                ],
                "prompt_head": "q",
            }
        )
    )
    out = asyncio.run(
        fake._h_consume_pending_recall({"session_id": "sess-C", "dedupe": True})
    )
    assert out is not None
    assert out["items"][0]["short_id"] == "cccc55556666"
    out_again = asyncio.run(
        fake._h_consume_pending_recall({"session_id": "sess-C", "dedupe": True})
    )
    assert out_again is None


def test_daemon_client_recall_fallback_when_daemon_unavailable(tmp_path, monkeypatch) -> None:
    from limem import daemon_client

    pending_path = tmp_path / "pending_recalls.json"
    monkeypatch.setattr(daemon_client, "PENDING_RECALLS_PATH", pending_path)
    monkeypatch.setattr(daemon_client, "ensure_or_spawn", lambda **__: False)
    monkeypatch.setattr(daemon_client, "safe_call", lambda *_, **__: None)

    payload = {
        "ts": 1700000000,
        "session_id": "sess-fallback",
        "project_id": "proj",
        "scope": "global",
        "items": [
            {
                "short_id": "aaaa11112222",
                "event_id": "e1",
                "src": "hard",
                "mem_type": "rule",
                "scope": "global",
                "summary_head": "提交前更新版本号",
            }
        ],
        "via_patterns": [],
        "via_keywords": ["提交"],
        "prompt_head": "提交前检查",
        "injected_chars": 100,
    }
    daemon_client.report_recall(payload)

    record = daemon_client.consume_pending_recall("sess-fallback")
    assert record is not None
    assert record["items"][0]["summary_head"] == "提交前更新版本号"
    assert daemon_client.consume_pending_recall("sess-fallback") is None


def test_format_stop_recall_systemmessage_with_short_ids() -> None:
    from limem import hooks as hmod

    record = {
        "items": [
            {"short_id": "aaa111aaa111", "src": "hard", "summary_head": "不要运行 npm dev"},
            {"short_id": "bbb222bbb222", "src": "bm25", "summary_head": "Docker rebuild 流程"},
            {"short_id": "ccc333ccc333", "src": "hard", "summary_head": "第三条"},
        ]
    }
    out = hmod._format_stop_recall_systemmessage(record)
    assert out.startswith("> 📚 LiMem\n> 本次引用 3 条记忆")
    assert "> - 规则 #aaa111aaa111 不要运行 npm dev" in out
    assert "> - 语义 #bbb222bbb222 Docker rebuild 流程" in out
    assert "#aaa111aaa111" in out
    assert "#bbb222bbb222" in out
    assert "> - 规则 #ccc333ccc333 第三条" in out
    assert "另 1 条" not in out


def test_format_stop_recall_systemmessage_includes_longer_summary() -> None:
    from limem import hooks as hmod

    summary = (
        "以后在 limem-agent-plugin 项目里，提交和推送代码之前都要先更新版本号，"
        "并保持版本元数据同步。"
    )
    out = hmod._format_stop_recall_systemmessage(
        {"items": [{"short_id": "7db02aec7003", "src": "hard", "summary_head": summary}]}
    )

    assert "规则 #7db02aec7003" in out
    assert "提交和推送代码之前都要先更新版本号" in out
    assert "版本元数据同步" in out


def test_format_stop_recall_systemmessage_strips_metadata_tail() -> None:
    from limem import hooks as hmod

    out = hmod._format_stop_recall_systemmessage(
        {
            "items": [
                {
                    "short_id": "7db02aec7003",
                    "src": "hard",
                    "summary_head": "提交前更新版本号。（实体：版本号、提交和推送）",
                }
            ]
        }
    )

    assert "提交前更新版本号。" in out
    assert "实体" not in out


def test_format_stop_recall_systemmessage_pattern_only() -> None:
    """纯 pattern src（无 short_id）时显示 N 条记忆（pattern 切片）。"""
    from limem import hooks as hmod

    record = {
        "items": [
            {"short_id": "", "src": "pattern", "canonical": "project:demo", "heading": "部署"},
            {"short_id": "", "src": "pattern", "canonical": "user:gaooooosh", "heading": "偏好"},
        ]
    }
    out = hmod._format_stop_recall_systemmessage(record)
    assert "本次引用 2 条记忆" in out
    assert "档案 project:demo · 部署" in out
    assert "档案 user:gaooooosh · 偏好" in out


def test_format_stop_recall_systemmessage_empty() -> None:
    from limem import hooks as hmod

    assert hmod._format_stop_recall_systemmessage({"items": []}) == "> 📚 LiMem\n> 本次未引用记忆"
    assert hmod._format_stop_recall_systemmessage({}) == "> 📚 LiMem\n> 本次未引用记忆"


def test_format_prompt_recall_systemmessage_excludes_hook_logs() -> None:
    from limem import hooks as hmod

    out = hmod._format_prompt_recall_systemmessage(
        {
            "ts": 1700000000,
            "items": [
                {
                    "short_id": "aaa111aaa111",
                    "src": "hard",
                    "summary_head": "不要运行 npm dev",
                }
            ],
        }
    )
    assert out.startswith("> 📚 LiMem\n")
    assert "UserPromptSubmit" not in out
    assert "2023-" not in out
    assert "本次引用 1 条记忆" in out
    assert "规则 #aaa111aaa111 不要运行 npm dev" in out


def test_emit_inject_can_emit_independent_system_message(capsys) -> None:
    from limem import hooks as hmod

    hmod._emit_inject(
        "UserPromptSubmit",
        "<limem_memory>ctx</limem_memory>",
        system_message="📚 LiMem · 本次引用 1 条记忆",
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "decision" not in data
    assert data["systemMessage"] == "📚 LiMem · 本次引用 1 条记忆"
    assert data["suppressOutput"] is False
    assert data["hookSpecificOutput"]["additionalContext"] == "<limem_memory>ctx</limem_memory>"
    assert "本次引用" not in data["hookSpecificOutput"]["additionalContext"]


def test_codex_visible_recall_context_helper_removed() -> None:
    """对话内引用注入已彻底移除，辅助函数不应再存在。"""
    from limem import hooks as hmod

    assert not hasattr(hmod, "_codex_visible_recall_context")


def test_emit_stop_systemmessage_writes_json(capsys) -> None:
    from limem import hooks as hmod

    hmod._emit_stop_systemmessage("📚 LiMem · test")
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "decision" not in data
    assert data["systemMessage"] == "📚 LiMem · test"
    assert data["suppressOutput"] is False


def test_emit_stop_systemmessage_empty_text_writes_empty(capsys) -> None:
    """空 text → stdout 写空串，不输出 JSON（避免对 Claude Code 显示干扰）。"""
    from limem import hooks as hmod

    hmod._emit_stop_systemmessage("")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_stop_recall_message_returns_empty_when_no_session_id() -> None:
    from limem import hooks as hmod

    assert hmod._stop_recall_message("") == ""


def test_stop_recall_message_returns_empty_when_daemon_returns_none(monkeypatch) -> None:
    from limem import hooks as hmod

    monkeypatch.setattr(
        hmod.daemon_client, "consume_pending_recall", lambda *_, **__: None
    )
    # pause 关闭
    class _NotPaused:
        def is_active(self):
            return False

    monkeypatch.setattr(hmod, "read_pause_from_disk", lambda: _NotPaused())
    assert hmod._stop_recall_message("sess-x") == ""


def test_stop_recall_message_returns_empty_when_paused(monkeypatch) -> None:
    from limem import hooks as hmod

    class _Paused:
        def is_active(self):
            return True

    monkeypatch.setattr(hmod, "read_pause_from_disk", lambda: _Paused())
    # 即便 daemon 有 record，pause 中也不展示
    monkeypatch.setattr(
        hmod.daemon_client,
        "consume_pending_recall",
        lambda *_, **__: {"items": [{"short_id": "x", "src": "hard"}]},
    )
    assert hmod._stop_recall_message("sess-x") == ""


def test_hook_stop_claude_full_path(monkeypatch, capsys) -> None:
    """端到端：daemon 返回 record → hook 输出含 systemMessage 的 JSON。"""
    from limem import hooks as hmod

    monkeypatch.setattr(
        hmod.daemon_client,
        "consume_pending_recall",
        lambda *_, **__: {
            "items": [
                {"short_id": "abcd00001111", "src": "hard"},
                {"short_id": "ef0022223333", "src": "bm25"},
            ]
        },
    )

    class _NotPaused:
        def is_active(self):
            return False

    monkeypatch.setattr(hmod, "read_pause_from_disk", lambda: _NotPaused())

    from limem.config import Credentials, RuntimeConfig

    hmod._hook_stop_claude(
        "claude-code",
        {"session_id": "sess-z"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(),
    )
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "本次引用 2 条记忆" in data["systemMessage"]


def test_hook_stop_codex_notice_only_via_stdout_systemmessage(monkeypatch, capsys, tmp_path) -> None:
    """召回提示只走 stdout 的 systemMessage JSON；不再写 stderr（Codex 会把 hook
    stderr 当作错误日志展示）。"""
    from limem import hooks as hmod

    monkeypatch.setattr(hmod, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hmod, "_flush_codex_session", lambda *_, **__: None)
    monkeypatch.setattr(
        hmod.daemon_client,
        "consume_pending_recall",
        lambda *_, **__: {
            "items": [
                {
                    "short_id": "abcd00001111",
                    "src": "hard",
                    "summary_head": "部署后运行 docker:rebuild",
                }
            ]
        },
    )

    class _NotPaused:
        def is_active(self):
            return False

    monkeypatch.setattr(hmod, "read_pause_from_disk", lambda: _NotPaused())

    from limem.config import Credentials, RuntimeConfig

    hmod._hook_stop_codex(
        "codex",
        {"session_id": "sess-z"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(),
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "本次引用 1 条记忆" in data["systemMessage"]
    assert "#abcd00001111" in data["systemMessage"]
    # 关键：不再向 stderr 写任何内容
    assert captured.err == ""


def test_hook_stop_codex_emits_notice_before_flush(monkeypatch, capsys, tmp_path) -> None:
    """Flush failures/timeouts must not prevent the Stop recall notice."""
    from limem import hooks as hmod

    old = tmp_path / "old.ndjson"
    old.write_text('{"ts": 1, "kind": "user_prompt", "payload": {"content": "x"}}\n')
    monkeypatch.setattr(hmod, "SESSIONS_DIR", tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(hmod, "_flush_codex_session", _boom)
    monkeypatch.setattr(
        hmod.daemon_client,
        "consume_pending_recall",
        lambda *_, **__: {
            "items": [
                {
                    "short_id": "abcd00001111",
                    "src": "hard",
                    "summary_head": "部署后运行 docker:rebuild",
                }
            ]
        },
    )

    class _NotPaused:
        def is_active(self):
            return False

    monkeypatch.setattr(hmod, "read_pause_from_disk", lambda: _NotPaused())

    from limem.config import Credentials, RuntimeConfig

    hmod._hook_stop_codex(
        "codex",
        {"session_id": "sess-z"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(
            codex_session_observation_enabled=True,
            codex_stop_idle_seconds=0,
        ),
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "本次引用 1 条记忆" in data["systemMessage"]
    assert "部署后运行 docker:rebuild" in data["systemMessage"]
    assert captured.err == ""


def test_hook_stop_codex_skips_session_flush_by_default(
    monkeypatch, capsys, tmp_path
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials, RuntimeConfig

    flushed: list[str] = []
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(hmod, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hmod, "_stop_recall_message", lambda _sid: "")
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "project-x")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:project-x")
    monkeypatch.setattr(
        hmod,
        "emit_event",
        lambda kind, **kw: emitted.append({"kind": kind, **kw}),
    )
    monkeypatch.setattr(
        hmod,
        "_flush_codex_session",
        lambda p, *_args: flushed.append(str(p)),
    )
    old = tmp_path / "old-session.ndjson"
    old.write_text('{"ts": 1, "kind": "user_prompt", "payload": {"content": "x"}}\n')
    old_mtime = time.time() - 3600
    os.utime(old, (old_mtime, old_mtime))

    hmod._hook_stop_codex(
        "codex",
        {"session_id": "sess-default-off", "assistant_response": "assistant output"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(codex_stop_idle_seconds=0),
    )

    assert capsys.readouterr().out == ""
    assert flushed == []
    assert not (tmp_path / "sess-default-off.ndjson").exists()
    assert old.exists()
    assert [e["kind"] for e in emitted] == ["assistant_evidence"]
    assert emitted[0]["payload"]["content"] == "assistant output"


def test_hook_user_prompt_submit_skips_codex_session_buffer_by_default(
    monkeypatch, tmp_path, capsys
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials, RuntimeConfig

    monkeypatch.setattr(hmod, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "project-x")
    monkeypatch.setattr(hmod.daemon_client, "safe_call", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        hmod.daemon_client,
        "get_connectivity",
        lambda: {"state": "ok"},
    )
    monkeypatch.setattr(hmod, "_active_principals", lambda *_args, **_kw: [])
    monkeypatch.setattr(hmod, "_emit_event_safe", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod.daemon_client, "report_recall", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        hmod,
        "EntityIndex",
        lambda: type("_Idx", (), {"list_hard_recall": lambda *_args, **_kw: []})(),
    )

    hmod._hook_user_prompt_submit(
        "codex",
        {"session_id": "sess-default-off", "prompt": "不要把这条写进 session buffer"},
        Credentials(api_key="", db_id="", user_id="u"),
        RuntimeConfig(),
    )

    capsys.readouterr()
    assert not (tmp_path / "sess-default-off.ndjson").exists()


def test_user_prompt_submit_probes_recovery_from_network_degraded(
    monkeypatch, tmp_path, capsys
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials, RuntimeConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "project-x")
    monkeypatch.setattr(hmod.daemon_client, "safe_call", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        hmod.daemon_client,
        "get_connectivity",
        lambda: {"state": "degraded", "reason": "network timeout"},
    )
    connectivity_updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        hmod.daemon_client,
        "set_connectivity",
        lambda **kw: connectivity_updates.append(kw),
    )
    monkeypatch.setattr(hmod.daemon_client, "bump_hit", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod.daemon_client, "report_recall", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod, "_active_principals", lambda *_args, **_kw: [])
    monkeypatch.setattr(hmod, "_emit_event_safe", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        hmod,
        "EntityIndex",
        lambda: type("_Idx", (), {"list_hard_recall": lambda *_args, **_kw: []})(),
    )

    class _TaskRecall:
        prompt_text = "## Relevant Memory\n- [Context] backend recovered"

    class _Client:
        def __init__(self, *_, **__):
            pass

        def db_health(self, **_kw):
            return {"status": "ok"}

        def recall_for_task(self, *_args, **_kw):
            return _TaskRecall()

    monkeypatch.setattr(hmod, "LimemClient", _Client)

    hmod._hook_user_prompt_submit(
        "claude-code",
        {"session_id": "sess-recover", "prompt": "继续任务"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(hook_timeout_ms=200, patterns_recall_timeout_ms=20),
    )

    out = json.loads(capsys.readouterr().out)
    assert "backend recovered" in out["hookSpecificOutput"]["additionalContext"]
    assert "status=\"degraded\"" not in out.get("systemMessage", "")
    assert {"status": 200, "ok": True} in connectivity_updates


def test_user_prompt_submit_continues_when_entity_index_unavailable(
    monkeypatch, tmp_path, capsys
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials, RuntimeConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "project-x")
    monkeypatch.setattr(hmod.daemon_client, "safe_call", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod.daemon_client, "get_connectivity", lambda: None)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod.daemon_client, "bump_hit", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod.daemon_client, "report_recall", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod, "_emit_event_safe", lambda *_args, **_kw: None)

    class _BrokenIndex:
        def __init__(self):
            raise OSError("readonly cache")

    class _TaskRecall:
        prompt_text = "## Relevant Memory\n- [Context] fallback task recall"

    class _Client:
        def __init__(self, *_, **__):
            pass

        def recall_for_task(self, *_args, **_kw):
            return _TaskRecall()

    monkeypatch.setattr(hmod, "EntityIndex", _BrokenIndex)
    monkeypatch.setattr(hmod, "LimemClient", _Client)

    hmod._hook_user_prompt_submit(
        "claude-code",
        {"session_id": "sess-no-index", "prompt": "继续任务"},
        Credentials(api_key="k", db_id="db", user_id="u"),
        RuntimeConfig(),
    )

    out = json.loads(capsys.readouterr().out)
    assert "fallback task recall" in out["hookSpecificOutput"]["additionalContext"]
    assert "本次引用 1 条记忆" in out["systemMessage"]


def test_hook_main_swallows_internal_exception_without_schema_log_crash(
    monkeypatch, capsys
) -> None:
    from limem import hooks as hmod

    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        hmod,
        "_log",
        lambda event_name, tool, **fields: logged.append(
            {"event_name": event_name, "tool": tool, **fields}
        ),
    )
    monkeypatch.setattr(
        hmod,
        "_hook_user_prompt_submit",
        lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(hmod.Credentials, "load", lambda: hmod.Credentials())
    monkeypatch.setattr(hmod.RuntimeConfig, "load", lambda: hmod.RuntimeConfig())
    monkeypatch.setattr(hmod.ProjectConfig, "discover", lambda: None)
    monkeypatch.setattr("sys.stdin", type("_Stdin", (), {"read": lambda self: "{}"})())

    assert hmod.main(["claude-code", "UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""
    assert logged
    assert logged[-1]["event_name"] == "hook_exception"
    assert logged[-1]["hook_event"] == "UserPromptSubmit"
    assert "boom" in logged[-1]["traceback"]


def test_build_codex_evidence_packet_keeps_raw_timeline() -> None:
    from limem import hooks as hmod

    packet = hmod._build_codex_evidence_packet(
        [
            {
                "ts": 1700000000,
                "kind": "user_prompt",
                "payload": {
                    "role": "user",
                    "content": "不要替后端 LLM 总结，只提交观察材料。",
                },
            },
            {
                "ts": 1700000001,
                "kind": "assistant_response",
                "payload": {
                    "role": "assistant",
                    "content": "我会保留原始证据，不做额外总结。",
                },
            },
            {"ts": 1700000002, "kind": "stop", "payload": {"hook": "Stop"}},
        ],
        project_id="github.com/gaooooosh/limem-agent-plugin",
        tool="codex",
        source="codex:stop_flush",
    )

    assert packet.startswith("# Agent Observation Packet")
    assert "## Evidence Timeline" in packet
    assert "### 1. User Message" in packet
    assert "Ref: user_prompt:1:1700000000" in packet
    assert "不要替后端 LLM 总结，只提交观察材料。" in packet
    assert "### 2. Assistant Response" in packet
    assert "我会保留原始证据，不做额外总结。" in packet
    assert "### 3. Stop Hook" in packet
    assert "## Truncation" in packet
    assert "None" in packet
    assert "## Raw References" in packet
    assert "User Intent" not in packet
    assert "Key Points" not in packet
    assert "first_turn_ts" not in packet
    assert "session_id" not in packet


def test_build_codex_evidence_packet_neutrally_truncates_long_assistant() -> None:
    from limem import hooks as hmod

    long_text = "A" * 3000 + "MIDDLE_SHOULD_BE_OMITTED" + "Z" * 3000
    packet = hmod._build_codex_evidence_packet(
        [
            {
                "ts": 1700000000,
                "kind": "assistant_response",
                "payload": {
                    "role": "assistant",
                    "content": long_text,
                    "content_hash": "hash123",
                },
            },
        ],
        project_id="github.com/gaooooosh/limem-agent-plugin",
        tool="codex",
        source="codex:stop_flush",
    )

    assert "### 1. Assistant Response" in packet
    assert "Ref: assistant_response:hash123" in packet
    assert "[... neutral truncation: middle omitted ...]" in packet
    assert "MIDDLE_SHOULD_BE_OMITTED" not in packet
    assert "## Truncation" in packet
    assert "assistant_response:hash123" in packet
    assert "original=6024" in packet


def test_read_codex_last_assistant_response_from_rollout(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import RuntimeConfig

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sess_dir = tmp_path / "sessions" / "2026" / "05" / "22"
    sess_dir.mkdir(parents=True)
    rollout = sess_dir / "rollout-2026-05-22T11-30-06-sess-abc.jsonl"
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "第一条回复"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "最终回复"}],
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))

    out = hmod._read_codex_last_assistant_response("sess-abc", RuntimeConfig())

    assert out == "最终回复"


def test_hook_stop_codex_appends_assistant_response_from_rollout(
    monkeypatch, tmp_path, capsys
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials, RuntimeConfig

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(hmod, "SESSIONS_DIR", tmp_path / "limem-sessions")
    monkeypatch.setattr(hmod, "_stop_recall_message", lambda _sid: "")
    monkeypatch.setattr(hmod, "_flush_codex_session", lambda *_, **__: None)

    sess_dir = tmp_path / "codex-home" / "sessions" / "2026" / "05" / "22"
    sess_dir.mkdir(parents=True)
    rollout = sess_dir / "rollout-2026-05-22T11-30-06-sess-stop.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "agent final answer"},
            },
            ensure_ascii=False,
        )
    )

    hmod._hook_stop_codex(
        "codex",
        {"session_id": "sess-stop"},
        Credentials(api_key="", db_id="", user_id=""),
        RuntimeConfig(
            codex_session_observation_enabled=True,
            codex_stop_idle_seconds=999999,
        ),
    )
    capsys.readouterr()

    buf = tmp_path / "limem-sessions" / "sess-stop.ndjson"
    rows = [json.loads(line) for line in buf.read_text().splitlines()]
    assert rows[0]["kind"] == "assistant_response"
    assert rows[0]["payload"]["content"] == "agent final answer"
    assert rows[1]["kind"] == "stop"


def test_flush_codex_session_ingests_markdown_evidence_packet(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    captured: dict[str, Any] = {}

    class _Result:
        event_id = "evt_1"
        summary = "ok"

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):
            captured["data"] = data
            captured["timestamp"] = timestamp
            return _Result()

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)
    monkeypatch.setattr(hmod, "ensure_default_principals", lambda *_args, **_kw: None)
    monkeypatch.setattr(hmod, "EntityIndex", lambda: object())
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "github.com/gaooooosh/limem-agent-plugin")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:github.com/gaooooosh/limem-agent-plugin")

    buf = tmp_path / "sess-md.ndjson"
    rows = [
        {
            "ts": 1700000000,
            "kind": "user_prompt",
            "payload": {
                "role": "user",
                "content": "payload 正文使用 Markdown，但不要替后端总结。",
            },
        },
        {"ts": 1700000001, "kind": "stop", "payload": {"hook": "Stop"}},
    ]
    buf.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    data = captured["data"]
    assert data["limem_type"] == "session_observation"
    assert data["text"] == "Codex conversation evidence packet"
    assert data["detail"].startswith("# Agent Observation Packet")
    assert "payload 正文使用 Markdown，但不要替后端总结。" in data["detail"]
    assert "User Intent" not in data["detail"]
    assert "Key Points" not in data["detail"]
    assert "first_turn_ts" not in data["detail"]
    assert data["metadata"]["turn_count"] == 2
    assert data["metadata"]["first_event_ts"] == 1700000000
    assert data["idempotency_key"].startswith("codex-stop-flush:")
    assert data["metadata"]["idempotency_key"] == data["idempotency_key"]
    assert data["metadata"]["event_start_index"] == 0
    assert data["metadata"]["event_end_index"] == 2
    assert data["metadata"]["packet_format"] == "turn_observation_neutral_pack_v1"
    assert data["metadata"]["packet_budget_chars"] == 12000
    assert not buf.exists()


def test_flush_codex_session_skips_invalid_binary_lines(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    captured: dict[str, Any] = {}

    class _Result:
        event_id = "evt_1"
        summary = "ok"

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            captured["data"] = data
            return _Result()

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "github.com/gaooooosh/limem-agent-plugin")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:github.com/gaooooosh/limem-agent-plugin")

    buf = tmp_path / "sess-binary.ndjson"
    valid = json.dumps(
        {
            "ts": 1700000000,
            "kind": "user_prompt",
            "payload": {"role": "user", "content": "valid prompt"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    buf.write_bytes(b"\x00\x00\x00\n" + valid + b"\n\xb3\x17j\x00")

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    assert "valid prompt" in captured["data"]["detail"]
    assert captured["data"]["metadata"]["turn_count"] == 1
    assert not buf.exists()


def test_flush_codex_session_ensures_default_principals_before_ingest(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    order: list[str] = []
    ensured: dict[str, Any] = {}

    class _Result:
        event_id = "evt_1"
        summary = "ok"

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            order.append("ingest")
            return _Result()

    def _ensure(creds, *, project_id, tool, idx, client=None, **kwargs):  # noqa: ARG001
        order.append("ensure")
        ensured.update(
            {
                "project_id": project_id,
                "tool": tool,
                "include_user": kwargs.get("include_user"),
                "include_agent": kwargs.get("include_agent"),
                "include_project": kwargs.get("include_project"),
                "client_type": type(client).__name__,
            }
        )
        return ["principal_project_x"]

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod, "ensure_default_principals", _ensure)
    monkeypatch.setattr(hmod, "EntityIndex", lambda: object())
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "github.com/gaooooosh/limem-agent-plugin")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:github.com/gaooooosh/limem-agent-plugin")

    buf = tmp_path / "sess-ensure.ndjson"
    buf.write_text(
        json.dumps(
            {
                "ts": 1700000000,
                "kind": "user_prompt",
                "payload": {"role": "user", "content": "hello"},
            },
            ensure_ascii=False,
        )
    )

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    assert order == ["ensure", "ingest"]
    assert ensured == {
        "project_id": "github.com/gaooooosh/limem-agent-plugin",
        "tool": "codex",
        "include_user": True,
        "include_agent": True,
        "include_project": True,
        "client_type": "_Client",
    }


def test_flush_codex_session_only_ingests_new_events_after_prior_flush(
    monkeypatch, tmp_path
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    captured: list[dict[str, Any]] = []

    class _Result:
        event_id = "evt_1"
        summary = "ok"

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            captured.append(data)
            return _Result()

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "github.com/gaooooosh/limem-agent-plugin")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:github.com/gaooooosh/limem-agent-plugin")

    buf = tmp_path / "sess-incremental.ndjson"
    first_rows = [
        {
            "ts": 1700000000,
            "kind": "user_prompt",
            "payload": {"role": "user", "content": "first prompt"},
        },
        {"ts": 1700000001, "kind": "stop", "payload": {"hook": "Stop"}},
    ]
    buf.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in first_rows))

    state = {
        "session_id": "sess-incremental",
        "first_event_ts": 1700000000,
        "submitted_line_count": 2,
        "last_idempotency_key": "codex-stop-flush:old",
        "status": "uncertain",
        "updated_ts": 1700000002,
    }
    (tmp_path / "sess-incremental.ndjson.flush.json").write_text(
        json.dumps(state, ensure_ascii=False)
    )
    next_rows = [
        *first_rows,
        {
            "ts": 1700000003,
            "kind": "user_prompt",
            "payload": {"role": "user", "content": "second prompt"},
        },
        {"ts": 1700000004, "kind": "stop", "payload": {"hook": "Stop"}},
    ]
    buf.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in next_rows))

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    assert len(captured) == 1
    data = captured[0]
    assert "second prompt" in data["detail"]
    assert "first prompt" not in data["detail"]
    assert data["metadata"]["turn_count"] == 2
    assert data["metadata"]["event_start_index"] == 2
    assert data["metadata"]["event_end_index"] == 4
    assert data["idempotency_key"].startswith("codex-stop-flush:")
    assert not buf.exists()


def test_flush_codex_session_records_uncertain_state_to_avoid_duplicate_retry(
    monkeypatch, tmp_path
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            raise RuntimeError("timeout after submit")

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)
    monkeypatch.setattr(hmod, "detect_project_id", lambda: "github.com/gaooooosh/limem-agent-plugin")
    monkeypatch.setattr(hmod, "project_scope", lambda: "project:github.com/gaooooosh/limem-agent-plugin")

    buf = tmp_path / "sess-uncertain.ndjson"
    buf.write_text(
        json.dumps(
            {
                "ts": 1700000000,
                "kind": "user_prompt",
                "payload": {"role": "user", "content": "maybe submitted"},
            },
            ensure_ascii=False,
        )
    )

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    state_path = tmp_path / "sess-uncertain.ndjson.flush.json"
    state = json.loads(state_path.read_text())
    assert state["submitted_line_count"] == 1
    assert state["status"] == "uncertain"
    assert state["last_idempotency_key"].startswith("codex-stop-flush:")


def test_flush_codex_session_keeps_uncertain_buffer_without_new_events(
    monkeypatch, tmp_path
) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    ingest_calls = 0

    class _Client:
        def __init__(self, **_kw):
            pass

        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            nonlocal ingest_calls
            ingest_calls += 1
            raise AssertionError("duplicate ingest should be skipped")

    monkeypatch.setattr(hmod, "LimemClient", _Client)
    monkeypatch.setattr(hmod.daemon_client, "set_connectivity", lambda **_kw: None)
    monkeypatch.setattr(hmod.session_mute, "clear", lambda _sid: None)

    buf = tmp_path / "sess-uncertain-repeat.ndjson"
    row = {
        "ts": 1700000000,
        "kind": "user_prompt",
        "payload": {"role": "user", "content": "already submitted maybe"},
    }
    buf.write_text(json.dumps(row, ensure_ascii=False))
    (tmp_path / "sess-uncertain-repeat.ndjson.flush.json").write_text(
        json.dumps(
            {
                "session_id": "sess-uncertain-repeat",
                "first_event_ts": 1700000000,
                "submitted_line_count": 1,
                "last_idempotency_key": "codex-stop-flush:old",
                "status": "uncertain",
                "updated_ts": 1700000002,
            },
            ensure_ascii=False,
        )
    )

    hmod._flush_codex_session(
        buf,
        Credentials(api_key="k", db_id="db", user_id="u"),
        "codex",
    )

    assert ingest_calls == 0
    assert buf.exists()
    assert (tmp_path / "sess-uncertain-repeat.ndjson.flush.json").exists()


def test_hook_report_recall_safe_completes_under_50ms(monkeypatch) -> None:
    """daemon 失败时整个上报路径仍快于 50ms（hook 预算）。"""
    from limem import hooks as hmod

    def _slow_failure(params):
        # 模拟 daemon_client.safe_call 失败但快速返回（safe_call 内部 200ms 上限，
        # 我们这里直接抛出，因为 _report_recall_safe 自带 try/except）
        raise RuntimeError("simulated")

    monkeypatch.setattr(hmod.daemon_client, "report_recall", _slow_failure)
    items = [
        InjectItem(
            kind="hard", score=1.0, event_id="e1", mem_type="rule",
            scope="global", summary="x", short_id="aaaa11112222",
        )
    ]
    t0 = time.time()
    hmod._report_recall_safe(
        rendered=items,
        session_id="s",
        project_id="p",
        scope="global",
        prompt="x",
        via_patterns=[],
        via_keywords=[],
        injected_chars=0,
    )
    elapsed_ms = (time.time() - t0) * 1000
    assert elapsed_ms < 50, f"上报路径耗时 {elapsed_ms:.1f}ms > 50ms"
