"""hooks: UserPromptSubmit 三路并发使用 principals 而非 entity FTS。"""

from __future__ import annotations

import pytest

from limem.entity_index import EntityIndex
from limem.injector import PatternRecallSlice


class _FakePatternRes:
    """模拟 client.patterns_recall 返回的同构响应对象。"""

    def __init__(self, content: str, headings: list[str]) -> None:
        from limem.client import MatchedSection

        self.mode = "section"
        self.content = content
        self.total_chars = len(content)
        self.matched_sections = [
            MatchedSection(heading=h, score=0.8, char_offset=0) for h in headings
        ]
        self.pattern = None

    def has_content(self) -> bool:
        return bool(self.content.strip())


def test_patterns_recall_called_once_per_active_principal(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod

    # 1) 准备 EntityIndex，注册三个 active principals
    db = tmp_path / "patterns.sqlite"
    idx = EntityIndex(db_path=db)
    idx.upsert_principal(
        entity_id="principal_project_deadbeef",
        principal_type="project",
        slug="foo/bar",
        canonical="project:bar",
        aliases=["bar"],
        description="测试项目",
        scope="project:foo/bar",
        project_id="foo/bar",
        has_pattern=True,
    )
    idx.upsert_principal(
        entity_id="principal_user_cafebabe",
        principal_type="user",
        slug="u_42",
        canonical="user:u_42",
        aliases=["我"],
        description="user",
        scope="global",
        has_pattern=True,
    )
    idx.upsert_principal(
        entity_id="principal_agent_codex",
        principal_type="agent",
        slug="codex",
        canonical="agent:codex",
        aliases=["你"],
        description="agent",
        scope="global",
        tool="codex",
        has_pattern=True,
    )

    # 2) Fake client：每个 entity_id 各返回一个 has_content=True 的切片
    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def __init__(self, *_, **__):
            pass

        def patterns_recall(self, entity_id, query, *, mode="section", top_k_sections=2, timeout=None):  # noqa: ARG002
            calls.append((entity_id, mode))
            return _FakePatternRes(content=f"## 命令规约\n- 来自 {entity_id}", headings=["命令规约"])

    monkeypatch.setattr(hmod, "LimemClient", _FakeClient)

    # 3) 触发 _patterns_recall_for_principals
    from limem.config import Credentials, RuntimeConfig

    creds = Credentials(api_key="k", db_id="db_1", user_id="u_42")
    runtime = RuntimeConfig.load()
    principals = idx.list_principals(active_only=True)
    slices = hmod._patterns_recall_for_principals(principals, "起一下 dev", creds, runtime)

    # 每个 principal 都被并发调用过一次
    assert len(calls) == 3
    assert {c[0] for c in calls} == {
        "principal_project_deadbeef",
        "principal_user_cafebabe",
        "principal_agent_codex",
    }
    # mode 始终是 "section"
    assert {c[1] for c in calls} == {"section"}
    # slices 内容含 canonical 形如 "<type>:<body>"
    assert len(slices) == 3
    assert all(isinstance(s, PatternRecallSlice) for s in slices)
    assert any(":" in s.canonical for s in slices)


def test_active_principals_lazy_ensures_when_empty(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    db = tmp_path / "patterns.sqlite"
    idx = EntityIndex(db_path=db)
    creds = Credentials(api_key="k", db_id="db_1", user_id="u_42")

    # patch ensure_default_principals 来注册一个 sentinel principal
    def _ensure(creds, *, project_id, tool, idx, client=None, force=False, **kwargs):  # noqa: ARG001
        idx.upsert_principal(
            entity_id="principal_user_cafebabe",
            principal_type="user",
            slug="u_42",
            canonical="user:u_42",
            aliases=[],
            description="",
            scope="global",
        )
        return ["principal_user_cafebabe"]

    monkeypatch.setattr(hmod, "ensure_default_principals", _ensure)

    out = hmod._active_principals(idx, creds, "foo/bar", "codex", lazy_ensure=True)
    assert any(p.entity_id == "principal_user_cafebabe" for p in out)


def test_active_principals_ensures_even_when_some_principals_exist(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    db = tmp_path / "patterns.sqlite"
    idx = EntityIndex(db_path=db)
    idx.upsert_principal(
        entity_id="principal_user_aaaaaaaa",
        principal_type="user",
        slug="u_x",
        canonical="user:u_x",
        scope="global",
        description="",
    )

    calls: list[int] = []

    def _ensure(creds, *, project_id, tool, idx, client=None, force=False, **kwargs):  # noqa: ARG001
        calls.append(1)
        idx.upsert_principal(
            entity_id="principal_agent_codex",
            principal_type="agent",
            slug="codex",
            canonical="agent:codex",
            scope="global",
            tool="codex",
            description="",
        )
        return ["principal_agent_codex"]

    monkeypatch.setattr(hmod, "ensure_default_principals", _ensure)

    creds = Credentials(api_key="k", db_id="db_1", user_id="u_x")
    out = hmod._active_principals(idx, creds, "foo/bar", "codex", lazy_ensure=True)
    assert calls == [1]
    assert any(p.entity_id == "principal_agent_codex" for p in out)


def test_active_principals_can_skip_agent_for_non_observer_paths(monkeypatch, tmp_path) -> None:
    from limem import hooks as hmod
    from limem.config import Credentials

    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    seen_kwargs: list[dict] = []

    def _ensure(creds, *, project_id, tool, idx, client=None, force=False, **kwargs):  # noqa: ARG001
        seen_kwargs.append(kwargs)
        return []

    monkeypatch.setattr(hmod, "ensure_default_principals", _ensure)

    creds = Credentials(api_key="k", db_id="db_1", user_id="u_x")
    hmod._active_principals(
        idx, creds, "foo/bar", "codex", lazy_ensure=True, include_agent=False
    )
    assert seen_kwargs[-1]["include_agent"] is False


def test_filter_query_results_downweights_principal_mismatch(tmp_path) -> None:
    """soft 召回里 principal 不匹配的项应被降权而非丢弃（用户决策）。"""
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    # 注入两条 event：一条 principal 匹配 active set，一条不匹配
    idx.upsert_event_metadata(
        {
            "event_id": "evt_match",
            "scope": "global",
            "mem_type": "note",
            "project_id": "",
            "importance": 0.5,
            "role": "",
            "source": "test",
            "ts": 1700000000,
            "summary": "match summary",
            "raw_metadata": {"principal_ids": ["principal_user_aaaa"]},
        }
    )
    idx.upsert_event_metadata(
        {
            "event_id": "evt_other",
            "scope": "global",
            "mem_type": "note",
            "project_id": "",
            "importance": 0.5,
            "role": "",
            "source": "test",
            "ts": 1700000000,
            "summary": "other summary",
            "raw_metadata": {"principal_ids": ["principal_user_zzzz"]},
        }
    )

    class _QR:
        def __init__(self, event_id, score):
            self.event_id = event_id
            self.score = score
            self.summary = ""

    results = [_QR("evt_match", 1.0), _QR("evt_other", 1.0)]
    kept = idx.filter_query_results(
        results,
        allowed_scopes={"global"},
        allowed_principals={"principal_user_aaaa"},
    )
    # 两条都保留（不丢弃）
    by_eid = {qr.event_id: qr for qr, _ in kept}
    assert set(by_eid.keys()) == {"evt_match", "evt_other"}
    # 不匹配项 score 减半
    assert by_eid["evt_match"].score == pytest.approx(1.0)
    assert by_eid["evt_other"].score == pytest.approx(0.5)
