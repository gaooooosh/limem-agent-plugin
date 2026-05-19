"""冒烟测试：所有模块可成功 import，关键签名存在。"""

from __future__ import annotations


def test_imports_all() -> None:
    import limem  # noqa
    from limem import (  # noqa: F401
        cli,
        client,
        config,
        daemon_client,
        exporter,
        hooks,
        injector,
        installer,
        mcp_server,
        memory_writer,
        migrate,
        notify,
        pattern_index,
        redact,
        scope,
        session_mute,
        statusline,
        tag_text,
    )
    from limem.daemon import (  # noqa: F401
        auto_init,
        connectivity,
        eventbus,
        jaccard,
        learner,
        lock,
        ngram,
        rpc,
        server,
        state,
        writer,
    )
    from limem.dash import app, keys  # noqa: F401


def test_render_inject_via_and_short_id() -> None:
    from limem.injector import InjectItem, render_inject

    items = [
        InjectItem(
            kind="hard",
            score=0.81,
            event_id="evt_abcdef1234567890",
            mem_type="rule",
            scope="project:foo/bar",
            summary="禁用 npm run dev，改 docker rebuild",
            importance=0.9,
            ts=1700000000,
            short_id="abc123def456",
        ),
    ]
    text = render_inject(items, via_patterns=["npm dev"], via_keywords=["docker", "rebuild"])
    assert 'via="entity:npm dev | bm25:docker rebuild"' in text
    assert "#abc123def456" in text
    assert "src=hard" in text


def test_entity_index_short_id_roundtrip(tmp_path) -> None:
    from limem.entity_index import EntityIndex

    db = tmp_path / "patterns.sqlite"
    idx = EntityIndex(db_path=db)
    short = idx.ensure_short_id("evt_xxxxxxxxxxxxxxxxxxxxxxxxx")
    assert len(short) >= 12
    same = idx.ensure_short_id("evt_xxxxxxxxxxxxxxxxxxxxxxxxx")
    assert same == short
    assert idx.lookup_event_by_short_id(short) == "evt_xxxxxxxxxxxxxxxxxxxxxxxxx"
    assert idx.lookup_event_by_short_id("#" + short) == "evt_xxxxxxxxxxxxxxxxxxxxxxxxx"


def test_jaccard_clusters() -> None:
    from limem.daemon.jaccard import cluster_by_similarity

    items = [
        ("不要用 npm run dev", {"id": 1}),
        ("别用 npm run dev", {"id": 2}),
        ("don't use npm run dev", {"id": 3}),
        ("完全不相关的文本", {"id": 4}),
    ]
    clusters = cluster_by_similarity(items, threshold=0.3)
    assert any(len(c) >= 2 for c in clusters)


def test_ngram_aggregate_thresholds() -> None:
    from limem.daemon.ngram import aggregate
    events = [{"diff_summary": "use docker compose up build", "accepted": True}] * 6
    events += [{"diff_summary": "noise", "accepted": False}]
    out = aggregate(events, min_occurrences=5, min_accept_rate=0.8, n=3)
    assert any("docker compose up" in r["ngram"] or "compose up build" in r["ngram"] for r in out)


def test_statusline_format_text_states() -> None:
    from limem.statusline import format_text

    t = format_text(
        active=7, hits=3, sug=2,
        pause_on=False, pause_until_ts=None,
        connectivity="healthy", reason=None,
        init_pending_until_ts=None, inited_now_ts=None,
    )
    assert "📚 7" in t and "▶ 3" in t and "💡 2" in t

    deg = format_text(
        active=0, hits=0, sug=0,
        pause_on=False, pause_until_ts=None,
        connectivity="degraded", reason="auth_expired",
        init_pending_until_ts=None, inited_now_ts=None,
    )
    assert "degraded" in deg


def test_clean_static_removes_block(tmp_path) -> None:
    from limem.migrate import clean_static
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "Other content top\n"
        "<!-- limem:rules begin -->\n"
        "some old rules\n"
        "<!-- limem:rules end -->\n"
        "Other content bottom\n"
    )
    res = clean_static(tmp_path)
    assert res.agents_md_cleaned
    new = agents.read_text()
    assert "limem:rules" not in new
    assert "Other content top" in new
    assert "Other content bottom" in new


def test_project_init_does_not_touch_agents_or_claude(tmp_path) -> None:
    from limem.installer import project_init
    (tmp_path / "AGENTS.md").write_text("# my project\n")
    (tmp_path / "CLAUDE.md").write_text("# claude rules\n")
    plan = project_init(tmp_path)
    assert plan.local_json_written
    # 关键断言：两个文件未被改动
    assert (tmp_path / "AGENTS.md").read_text() == "# my project\n"
    assert (tmp_path / "CLAUDE.md").read_text() == "# claude rules\n"


def test_pause_state_disk_roundtrip(tmp_path, monkeypatch) -> None:
    import limem.config as cfg
    import limem.daemon.state as state
    monkeypatch.setattr(cfg, "PAUSE_PATH", tmp_path / "pause.json")
    monkeypatch.setattr(state, "PAUSE_PATH", tmp_path / "pause.json")
    p = state.PauseState(on=True, until_ts=2**31 - 1, scope="project")
    p.save_to_disk()
    loaded = state.PauseState.load_from_disk()
    assert loaded.on is True
    assert loaded.scope == "project"


def test_learner_correction_detection() -> None:
    from limem.daemon.learner import extract_subject, is_correction
    assert is_correction("不对，应该用 docker")
    assert is_correction("Don't use npm dev")
    assert not is_correction("hello world")
    assert "npm" in extract_subject("npm 不对").lower() or extract_subject("npm 不对")


def test_learner_correction_suggestion_has_review_context() -> None:
    import time

    from limem.daemon.learner import run_correction_analyzer

    now = int(time.time())
    events = [
        {
            "ts": now - 60,
            "project_id": "github.com/example/repo",
            "scope": "project:github.com/example/repo",
            "prompt": "不要用 npm run dev，应该 docker rebuild",
            "session_id": "session-alpha",
            "tool": "codex",
            "evidence_id": "abcdef123456",
        },
        {
            "ts": now - 30,
            "project_id": "github.com/example/repo",
            "scope": "project:github.com/example/repo",
            "prompt": "别用 npm run dev，应该 docker rebuild",
            "session_id": "session-alpha",
            "tool": "codex",
            "evidence_id": "abcdef123457",
        },
    ]
    out = run_correction_analyzer(
        events,
        window_seconds=24 * 3600,
        jaccard_threshold=0.2,
    )
    assert len(out) == 1
    suggestion = out[0]
    assert suggestion["candidate_text"].startswith("在本项目中")
    assert "rationale" in suggestion
    assert suggestion["evidence"]
    assert "abcdef123456" in suggestion["evidence"][0]
    # Evidence remains review context, not part of the text that will be remembered.
    assert "abcdef123456" not in suggestion["candidate_text"]


def test_accept_suggestion_uses_candidate_text_only(monkeypatch, tmp_path) -> None:
    import limem.daemon.learner as learner
    import limem.daemon.server as server

    suggestions_path = tmp_path / "suggestions.json"
    monkeypatch.setattr(learner, "SUGGESTIONS_PATH", suggestions_path)
    monkeypatch.setattr(server, "load_suggestions", learner.load_suggestions)
    monkeypatch.setattr(server, "save_suggestions", learner.save_suggestions)
    learner.save_suggestions(
        [
            {
                "id": "sug_1",
                "kind": "rule",
                "scope": "project:demo",
                "candidate_text": "在本项目中，避免运行 npm run dev。",
                "rationale": "用户多次纠正。",
                "evidence": ["2026-01-01 [codex] #abc: 不要 npm run dev"],
                "status": "pending",
            }
        ]
    )

    captured = {}

    def fake_remember_impl(**kwargs):
        captured.update(kwargs)
        return {"event_id": "evt_1"}

    monkeypatch.setattr(server, "remember_impl", fake_remember_impl)

    import asyncio
    from types import SimpleNamespace

    daemon = SimpleNamespace(
        creds=None,
        runtime=None,
        pidx=None,
        state=SimpleNamespace(suggestion_count=1, active_memories=0),
    )
    result = asyncio.run(server.Daemon._h_accept_suggestion(daemon, {"id": "sug_1"}))
    assert result == {"event_id": "evt_1"}
    assert captured["text"] == "在本项目中，避免运行 npm run dev。"
    assert "用户多次纠正" not in captured["text"]
    assert "#abc" not in captured["text"]


def test_merge_suggestions_dedupes_learned_items() -> None:
    from limem.daemon.learner import merge_suggestions

    existing = [
        {
            "id": "sug_old",
            "kind": "rule",
            "scope": "project:demo",
            "candidate_text": "在本项目中，避免运行 npm run dev。",
            "status": "learned",
        }
    ]
    new = [
        {
            "id": "sug_new",
            "kind": "rule",
            "scope": "project:demo",
            "candidate_text": "在本项目中，避免运行 npm run dev。",
            "status": "pending",
        }
    ]

    merged = merge_suggestions(existing, new)
    assert len(merged) == 1
    assert merged[0]["id"] == "sug_old"


def test_passive_learning_submits_pending_suggestion(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    import limem.daemon.server as server

    captured = {}

    def fake_remember_impl(**kwargs):
        captured.update(kwargs)
        return {"event_id": "evt_passive_1"}

    monkeypatch.setattr(server, "remember_impl", fake_remember_impl)

    daemon = SimpleNamespace(
        creds=SimpleNamespace(api_key="key", db_id="db"),
        runtime=SimpleNamespace(),
        pidx=object(),
    )
    daemon._passive_learning_text = server.Daemon._passive_learning_text.__get__(daemon)
    daemon._passive_learning_detail = server.Daemon._passive_learning_detail.__get__(daemon)
    daemon._project_id_from_scope = server.Daemon._project_id_from_scope.__get__(daemon)
    items = [
        {
            "id": "sug_1",
            "kind": "rule",
            "scope": "project:demo",
            "candidate_text": "在本项目中，避免运行 npm run dev。",
            "rationale": "用户多次纠正。",
            "evidence": ["2026-01-01 [codex] #abc: 不要 npm run dev"],
            "confidence": 0.91,
            "status": "pending",
        }
    ]

    learned = asyncio.run(server.Daemon._submit_passive_suggestions(daemon, items))
    assert learned == 1
    assert items[0]["status"] == "learned"
    assert items[0]["learned_event_id"] == "evt_passive_1"
    assert captured["source"] == "daemon:passive_learning"
    assert captured["text"] == "在本项目中，避免运行 npm run dev。"
    assert captured["detail"].startswith("passive learning observation")
    assert captured["project_id"] == "demo"
