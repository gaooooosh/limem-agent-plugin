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
            event_id="evt_abcdef1234567890",
            mem_type="rule",
            scope="project:foo/bar",
            summary="禁用 npm run dev，改 docker rebuild",
            importance=0.9,
            ts=1700000000,
            source="pattern",
            short_id="abc123def456",
        ),
    ]
    text = render_inject(items, via_patterns=["npm dev"], via_keywords=["docker", "rebuild"])
    assert 'via="pattern:npm dev | bm25:docker rebuild"' in text
    assert "#abc123def456" in text
    assert "src=pattern" in text


def test_pattern_index_short_id_roundtrip(tmp_path, monkeypatch) -> None:
    db = tmp_path / "patterns.sqlite"
    from limem.pattern_index import PatternIndex

    pidx = PatternIndex(db_path=db)
    short = pidx.ensure_short_id("evt_xxxxxxxxxxxxxxxxxxxxxxxxx")
    assert len(short) >= 12
    same = pidx.ensure_short_id("evt_xxxxxxxxxxxxxxxxxxxxxxxxx")
    assert same == short
    assert pidx.lookup_event_by_short_id(short) == "evt_xxxxxxxxxxxxxxxxxxxxxxxxx"
    assert pidx.lookup_event_by_short_id("#" + short) == "evt_xxxxxxxxxxxxxxxxxxxxxxxxx"


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
    from limem.daemon.learner import is_correction, extract_subject
    assert is_correction("不对，应该用 docker")
    assert is_correction("Don't use npm dev")
    assert not is_correction("hello world")
    assert "npm" in extract_subject("npm 不对").lower() or extract_subject("npm 不对")
