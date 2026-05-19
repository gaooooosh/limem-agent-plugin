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

    # 本地 event_metadata.raw_metadata 完整
    meta = idx.lookup_event(out["event_id"])
    assert meta is not None
    assert meta.raw_metadata.get("canonicals") == ["npm run dev", "docker compose up --build"]
    assert meta.raw_metadata.get("principal_ids") == out["principal_ids"]
    assert meta.raw_metadata.get("original_text", "").startswith("禁用 npm run dev")


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
