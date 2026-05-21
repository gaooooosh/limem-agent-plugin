from __future__ import annotations


def test_ensure_default_principals_retries_when_local_backend_failed(monkeypatch, tmp_path) -> None:
    from limem.config import Credentials
    from limem.entity_index import EntityIndex
    from limem.principals import (
        PrincipalSpec,
        ensure_default_principals,
        entity_id_for,
    )
    import limem.principals as pmod

    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    project_id = "github.com/foo/bar"
    spec = PrincipalSpec(
        principal_type="project",
        slug=project_id,
        project_id=project_id,
        description="",
    )
    eid = entity_id_for(spec)
    idx.upsert_principal(
        entity_id=eid,
        principal_type="project",
        slug=project_id,
        canonical="project:bar",
        aliases=[project_id, "bar"],
        description="",
        scope=f"project:{project_id}",
        project_id=project_id,
        raw_metadata={"backend_ok": False},
    )

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / eid).touch()
    monkeypatch.setattr(pmod, "_ENSURE_MARKER_DIR", marker_dir)

    calls: list[str] = []

    def _register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        calls.append(spec.principal_type)
        idx.upsert_principal(
            entity_id=entity_id_for(spec),
            principal_type=spec.principal_type,
            slug=spec.slug,
            canonical=spec.normalized_canonical(),
            aliases=spec.aliases,
            description=spec.description,
            scope=spec.scope,
            tool=spec.tool,
            project_id=spec.project_id,
            raw_metadata={"backend_ok": True},
        )
        return entity_id_for(spec)

    monkeypatch.setattr(pmod, "register_principal", _register)

    out = ensure_default_principals(
        Credentials(api_key="k", db_id="db", user_id=""),
        project_id=project_id,
        tool="",
        idx=idx,
        include_user=False,
        include_agent=False,
        include_project=True,
    )

    assert out == [eid]
    assert calls == ["project"]
    assert idx.lookup_principal(eid).raw_metadata["backend_ok"] is True


def test_ensure_default_principals_does_not_mark_failed_backend(monkeypatch, tmp_path) -> None:
    from limem.config import Credentials
    from limem.entity_index import EntityIndex
    from limem.principals import ensure_default_principals, entity_id_for, PrincipalSpec
    import limem.principals as pmod

    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    project_id = "github.com/foo/bar"
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(pmod, "_ENSURE_MARKER_DIR", marker_dir)

    def _register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        eid = entity_id_for(spec)
        idx.upsert_principal(
            entity_id=eid,
            principal_type=spec.principal_type,
            slug=spec.slug,
            canonical=spec.normalized_canonical(),
            description=spec.description,
            scope=spec.scope,
            project_id=spec.project_id,
            raw_metadata={"backend_ok": False},
        )
        return eid

    monkeypatch.setattr(pmod, "register_principal", _register)

    out = ensure_default_principals(
        Credentials(api_key="k", db_id="db", user_id=""),
        project_id=project_id,
        tool="",
        idx=idx,
        include_user=False,
        include_agent=False,
        include_project=True,
    )

    eid = entity_id_for(
        PrincipalSpec("project", slug=project_id, project_id=project_id, description="")
    )
    assert out == [eid]
    assert not (marker_dir / eid).exists()


def test_ensure_default_principals_resolves_missing_user_id_from_me(monkeypatch, tmp_path) -> None:
    from limem.config import Credentials
    from limem.entity_index import EntityIndex
    from limem.principals import PrincipalSpec, ensure_default_principals, entity_id_for
    import limem.principals as pmod

    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(pmod, "_ENSURE_MARKER_DIR", marker_dir)
    creds = Credentials(api_key="k", db_id="db", user_id="")
    saved: list[str] = []
    calls: list[str] = []

    class FakeClient:
        def me(self):
            return {"user_id": "u_42"}

    def _save():
        saved.append(creds.user_id)

    def _register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        calls.append(spec.principal_type)
        eid = entity_id_for(spec)
        idx.upsert_principal(
            entity_id=eid,
            principal_type=spec.principal_type,
            slug=spec.slug,
            canonical=spec.normalized_canonical(),
            aliases=spec.aliases,
            description=spec.description,
            scope=spec.scope,
            tool=spec.tool,
            project_id=spec.project_id,
            raw_metadata={"backend_ok": True},
        )
        return eid

    monkeypatch.setattr(creds, "save", _save)
    monkeypatch.setattr(pmod, "register_principal", _register)

    out = ensure_default_principals(
        creds,
        project_id="github.com/foo/bar",
        tool="codex",
        idx=idx,
        client=FakeClient(),
    )

    user_eid = entity_id_for(PrincipalSpec("user", slug="u_42", description=""))
    assert out[0] == user_eid
    assert creds.user_id == "u_42"
    assert saved == ["u_42"]
    assert calls == ["user", "agent", "project"]
    assert idx.lookup_principal(user_eid).principal_type == "user"
