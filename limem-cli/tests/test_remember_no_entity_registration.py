"""v3：remember 写入 event 后**不再**注册后端 entity（mention 仅作 metadata）。"""

from __future__ import annotations

from limem.daemon.writer import remember_impl
from limem.entity_index import EntityIndex


class _FakeIngestResult:
    def __init__(self, event_id: str, summary: str) -> None:
        self.event_id = event_id
        self.summary = summary
        self.is_new = True
        self.entities_created = 0
        self.event_count = 1


class _FakeClient:
    """记录所有方法调用；ingest 返回预设 event_id；entity_* 方法计数应保持 0。"""

    def __init__(self, *, user_id: str = "u_42") -> None:
        self.ingest_calls: list[dict] = []
        self.entity_create_calls: list[dict] = []
        self.entity_patch_calls: list[dict] = []
        self.creds_user_id = user_id

    def ingest(self, data, *, timestamp=None):  # noqa: ARG002
        self.ingest_calls.append(data)
        return _FakeIngestResult(
            event_id=f"evt_{len(self.ingest_calls):08d}",
            summary=(data.get("text") or "")[:100],
        )

    def me(self):
        return {"user_id": self.creds_user_id}

    def entity_create_or_promote(self, *args, **kwargs):  # noqa: ARG002
        self.entity_create_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("v3: remember_impl 不应注册后端 entity")

    def entity_patch(self, *args, **kwargs):  # noqa: ARG002
        self.entity_patch_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("v3: remember_impl 不应 PATCH entity")


class _FakeCreds:
    api_key = "k"
    db_id = "db_1"
    user_id = "u_42"


def _patch(monkeypatch, fake_client: _FakeClient) -> None:
    """让所有 `LimemClient(...)` 都返回同一个 fake；同时绕过 redact 校验。"""
    from limem import daemon
    from limem.daemon import writer as wmod

    monkeypatch.setattr(wmod, "LimemClient", lambda **_kw: fake_client)
    # ensure_default_principals 调本身的 _LimemClient（local import in principals.py），
    # 同时 swallow=True 默认不会传播错误；我们再把 principals.register_principal 直接 noop
    from limem import principals as pmod

    def _noop_register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        from limem.principals import entity_id_for

        return entity_id_for(spec)

    monkeypatch.setattr(pmod, "register_principal", _noop_register)
    _ = daemon  # keep linter happy


def test_remember_writes_event_but_no_entity_registration(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    idx.upsert_principal(
        entity_id="principal_agent_codex",
        principal_type="agent",
        slug="codex",
        canonical="agent:codex",
        aliases=[],
        description="",
        scope="global",
        tool="codex",
    )

    out = remember_impl(
        text="禁用 npm run dev，用 docker compose up --build",
        scope="project:foo/bar",
        mem_type="rule",
        importance=0.9,
        project_id="foo/bar",
        entities=[
            {"canonical": "npm run dev", "role": "forbidden", "aliases": ["npm dev"]},
            {"canonical": "docker compose up --build", "role": "preferred", "aliases": []},
        ],
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    # 后端：只有 1 次 ingest，0 次 entity 注册 / patch
    assert len(fake.ingest_calls) == 1
    assert fake.entity_create_calls == []
    assert fake.entity_patch_calls == []

    # 返回值含 v3 字段
    assert out["event_id"].startswith("evt_")
    assert out["canonicals"] == ["npm run dev", "docker compose up --build"]
    assert out["entities_registered"] == []  # 显式空
    # principal_ids 包含 project principal
    assert any(p.startswith("principal_project_") for p in out["principal_ids"])
    assert out["observer_principal_id"] == "principal_agent_codex"
    assert any(p.startswith("principal_project_") for p in out["subject_principal_ids"])

    # 本地 event_metadata.raw_metadata 完整
    meta = idx.lookup_event(out["event_id"])
    assert meta is not None
    assert meta.raw_metadata.get("canonicals") == ["npm run dev", "docker compose up --build"]
    assert meta.raw_metadata.get("principal_ids") == out["principal_ids"]
    assert meta.raw_metadata.get("observer_principal_id") == "principal_agent_codex"
    assert meta.raw_metadata.get("subject_principal_ids") == out["subject_principal_ids"]
    assert meta.raw_metadata.get("original_text", "").startswith("禁用 npm run dev")


def test_remember_ingest_detail_is_natural_context(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    remember_impl(
        text="禁止直接启动本地 dev server。",
        scope="project:foo/bar",
        mem_type="rule",
        importance=0.9,
        project_id="foo/bar",
        session_id="sess_1",
        source="codex:test",
        detail="用户明确要求 Docker 优先。",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    detail = fake.ingest_calls[0]["detail"]
    assert "现在的情况是：" in detail
    assert "当前项目是 foo/bar" in detail
    assert "工具来源是 codex:test" in detail
    assert "会话是 sess_1" in detail
    assert "具体发生的内容是：用户明确要求 Docker 优先。" in detail


def test_global_remember_subject_includes_resolved_user_principal(monkeypatch, tmp_path) -> None:
    fake = _FakeClient(user_id="u_from_me")
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    class CredsWithoutUser:
        api_key = "k"
        db_id = "db_1"
        user_id = ""

        def save(self):
            pass

    creds = CredsWithoutUser()
    out = remember_impl(
        text="Always prefer concise summaries.",
        scope="global",
        mem_type="preference",
        importance=0.8,
        creds=creds,
        idx=idx,
        skip_redact=True,
    )

    assert creds.user_id == "u_from_me"
    assert any(p.startswith("principal_user_") for p in out["subject_principal_ids"])
    assert out["subject_principal_ids"] == out["principal_ids"]


def test_ten_consecutive_remembers_do_not_inflate_entities(monkeypatch, tmp_path) -> None:
    """连续 10 次 remember 不同 canonical，后端 entity 注册次数仍是 0。"""
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    canonicals = [
        "npm run dev", "yarn dev", "pnpm dev", "bun dev",
        "docker compose", "docker rebuild", "make build",
        "tsc --watch", "vite", "next dev",
    ]
    for i, c in enumerate(canonicals):
        remember_impl(
            text=f"avoid {c} (#{i})",
            scope="project:foo/bar",
            mem_type="rule",
            importance=0.8,
            project_id="foo/bar",
            entities=[{"canonical": c, "role": "forbidden"}],
            creds=_FakeCreds(),
            idx=idx,
            skip_redact=True,
        )

    assert len(fake.ingest_calls) == 10
    assert fake.entity_create_calls == []
    assert fake.entity_patch_calls == []


def test_memory_writer_falls_back_only_when_daemon_did_not_accept(monkeypatch, tmp_path) -> None:
    from limem import daemon_client
    from limem import memory_writer as mw

    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    monkeypatch.setattr(daemon_client, "write_memory", lambda _params: None)

    res = mw.remember(
        text="fallback write",
        scope="project:foo/bar",
        mem_type="rule",
        project_id="foo/bar",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    assert res.event_id == "evt_00000001"
    assert len(fake.ingest_calls) == 1


def test_daemon_write_memory_uses_longer_timeout(monkeypatch) -> None:
    from limem import daemon_client
    from limem.config import RuntimeConfig

    calls = []

    def fake_call(method, params=None, *, connect_timeout_ms=25, call_timeout_ms=200):
        calls.append(
            {
                "method": method,
                "params": params,
                "connect_timeout_ms": connect_timeout_ms,
                "call_timeout_ms": call_timeout_ms,
            }
        )
        return {"event_id": "evt_1"}

    monkeypatch.setattr(
        RuntimeConfig,
        "load",
        lambda: RuntimeConfig(daemon_connect_timeout_ms=31, daemon_write_timeout_ms=7000),
    )
    monkeypatch.setattr(daemon_client, "call", fake_call)

    assert daemon_client.write_memory({"text": "feedback"}) == {"event_id": "evt_1"}
    assert calls == [
        {
            "method": "write_memory",
            "params": {"text": "feedback"},
            "connect_timeout_ms": 31,
            "call_timeout_ms": 7000,
        }
    ]


def test_memory_writer_does_not_fallback_when_daemon_result_is_uncertain(monkeypatch, tmp_path) -> None:
    from limem import daemon_client
    from limem import memory_writer as mw
    from limem.client import LimemError

    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    def uncertain(_params):
        raise daemon_client.DaemonCallUncertain("timed out after send")

    monkeypatch.setattr(daemon_client, "write_memory", uncertain)

    try:
        mw.remember(
            text="do not duplicate",
            scope="project:foo/bar",
            mem_type="rule",
            project_id="foo/bar",
            creds=_FakeCreds(),
            idx=idx,
            skip_redact=True,
        )
    except LimemError as e:
        assert "not falling back to local ingest" in str(e)
    else:
        raise AssertionError("expected uncertain daemon write to stop without fallback")

    assert fake.ingest_calls == []


def test_remember_impl_dedupes_same_memory_write_key(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    first = remember_impl(
        text="禁止重复发同一条记忆。",
        scope="project:foo/bar",
        mem_type="feedback",
        importance=0.85,
        project_id="foo/bar",
        source="mcp:limem_write",
        entities=[{"canonical": "重复写入", "role": "forbidden"}],
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )
    second = remember_impl(
        text="禁止重复发同一条记忆。",
        scope="project:foo/bar",
        mem_type="feedback",
        importance=0.85,
        project_id="foo/bar",
        source="daemon:passive_learning",
        entities=[{"canonical": "重复写入", "role": "forbidden"}],
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    assert len(fake.ingest_calls) == 1
    assert second["deduped"] is True
    assert second["event_id"] == first["event_id"]
    assert second["write_key"] == first["write_key"]


def test_remember_impl_does_not_dedupe_different_memory_text(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    remember_impl(
        text="第一条记忆。",
        scope="project:foo/bar",
        mem_type="rule",
        project_id="foo/bar",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )
    remember_impl(
        text="第二条记忆。",
        scope="project:foo/bar",
        mem_type="rule",
        project_id="foo/bar",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    assert len(fake.ingest_calls) == 2


def test_remember_impl_blocks_duplicate_when_first_write_is_uncertain(monkeypatch, tmp_path) -> None:
    from limem.client import LimemError

    class FailingClient(_FakeClient):
        def ingest(self, data, *, timestamp=None):  # noqa: ARG002
            self.ingest_calls.append(data)
            raise LimemError(0, "network timeout")

    fake = FailingClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    try:
        remember_impl(
            text="这条写入结果未知。",
            scope="project:foo/bar",
            mem_type="rule",
            project_id="foo/bar",
            creds=_FakeCreds(),
            idx=idx,
            skip_redact=True,
        )
    except LimemError:
        pass
    else:
        raise AssertionError("expected first ingest to fail")

    try:
        remember_impl(
            text="这条写入结果未知。",
            scope="project:foo/bar",
            mem_type="rule",
            project_id="foo/bar",
            creds=_FakeCreds(),
            idx=idx,
            skip_redact=True,
        )
    except LimemError as e:
        assert "not issuing duplicate ingest" in str(e)
    else:
        raise AssertionError("expected duplicate uncertain write to be blocked")

    assert len(fake.ingest_calls) == 1
