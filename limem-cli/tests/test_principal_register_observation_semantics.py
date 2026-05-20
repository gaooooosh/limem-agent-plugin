"""Principal registration follows observation semantics."""

from __future__ import annotations

import json

from limem.config import Credentials


def test_mcp_principal_register_ensures_user_but_not_agent(monkeypatch, tmp_path) -> None:
    from limem import mcp_server as mcp
    from limem.entity_index import EntityIndex

    monkeypatch.setattr(mcp, "EntityIndex", lambda: EntityIndex(db_path=tmp_path / "patterns.sqlite"))
    monkeypatch.setattr(mcp.Credentials, "load", classmethod(lambda cls: Credentials(api_key="k", db_id="db", user_id="u_42")))

    ensured: list[str] = []
    registered: list[str] = []

    def _ensure_user(creds, *, idx, client=None, force=False):  # noqa: ARG001
        ensured.append(creds.user_id)
        return "principal_user_2b2a9e9e"

    def _register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        registered.append(spec.principal_type)
        return f"principal_{spec.principal_type}_{spec.slug}"

    monkeypatch.setattr(mcp, "ensure_current_user_principal", _ensure_user)
    monkeypatch.setattr(mcp, "register_principal", _register)

    out = json.loads(
        mcp._t_principal_register(
            "service",
            "billing",
            "Billing service",
            aliases=["账单服务"],
            scope="global",
        )
    )

    assert ensured == ["u_42"]
    assert registered == ["service"]
    assert out["ensured_user_principal_id"] == "principal_user_2b2a9e9e"


def test_mcp_search_does_not_guess_agent_principal(monkeypatch, tmp_path) -> None:
    from limem import mcp_server as mcp
    from limem.entity_index import EntityIndex

    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    monkeypatch.setattr(mcp, "EntityIndex", lambda: idx)
    monkeypatch.setattr(mcp.Credentials, "load", classmethod(lambda cls: Credentials(api_key="k", db_id="db", user_id="u_42")))
    monkeypatch.setattr(mcp, "detect_project_id", lambda: "foo/bar")

    kwargs_seen: list[dict] = []

    def _ensure(creds, *, project_id, tool, idx, client=None, force=False, **kwargs):  # noqa: ARG001
        kwargs_seen.append({"tool": tool, **kwargs})
        return []

    monkeypatch.setattr(mcp, "ensure_default_principals", _ensure)

    mcp._t_search("anything", include_patterns=True)

    assert kwargs_seen
    assert kwargs_seen[-1]["tool"] == ""
    assert kwargs_seen[-1]["include_agent"] is False
