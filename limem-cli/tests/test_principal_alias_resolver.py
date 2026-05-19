"""principal alias 解析：``project / user / agent`` → stable entity_id。"""

from __future__ import annotations

from dataclasses import dataclass

from limem.entity_index import EntityIndex
from limem.principals import PrincipalSpec, entity_id_for, principal_alias_to_id, sha8


@dataclass
class _Creds:
    user_id: str = "u_42"


def test_alias_user_maps_to_stable_id() -> None:
    creds = _Creds(user_id="u_42")
    eid = principal_alias_to_id(
        "user", creds=creds, project_id="github.com/foo/bar", tool="codex"
    )
    assert eid == f"principal_user_{sha8('u_42')}"


def test_alias_agent_maps_to_tool_slug() -> None:
    creds = _Creds()
    eid = principal_alias_to_id(
        "agent", creds=creds, project_id="", tool="codex"
    )
    assert eid == "principal_agent_codex"


def test_alias_project_maps_to_sha_of_project_id() -> None:
    creds = _Creds()
    eid = principal_alias_to_id(
        "project", creds=creds, project_id="github.com/foo/bar", tool="codex"
    )
    assert eid == f"principal_project_{sha8('github.com/foo/bar')}"


def test_stable_id_unchanged_when_already_principal_prefix(tmp_path) -> None:
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    creds = _Creds()
    raw = "principal_team_platform"
    assert (
        principal_alias_to_id(raw, creds=creds, project_id="", tool="", idx=idx) == raw
    )


def test_falls_back_to_local_principal_lookup(tmp_path) -> None:
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    idx.upsert_principal(
        entity_id="principal_team_platform",
        principal_type="team",
        slug="platform",
        canonical="team:platform",
        aliases=["平台", "Platform Team"],
        description="平台团队",
        scope="global",
    )
    creds = _Creds()
    # alias 命中
    assert (
        principal_alias_to_id("平台", creds=creds, project_id="", tool="", idx=idx)
        == "principal_team_platform"
    )
    # slug 命中
    assert (
        principal_alias_to_id("platform", creds=creds, project_id="", tool="", idx=idx)
        == "principal_team_platform"
    )
    # canonical 命中
    assert (
        principal_alias_to_id("team:platform", creds=creds, project_id="", tool="", idx=idx)
        == "principal_team_platform"
    )


def test_entity_id_for_is_stable_across_runs() -> None:
    spec_a = PrincipalSpec(
        principal_type="project", slug="github.com/foo/bar", description=""
    )
    spec_b = PrincipalSpec(
        principal_type="project", slug="github.com/foo/bar", description=""
    )
    assert entity_id_for(spec_a) == entity_id_for(spec_b)


def test_unknown_alias_returns_original() -> None:
    creds = _Creds()
    raw = "some_canonical_that_is_not_a_principal"
    assert (
        principal_alias_to_id(raw, creds=creds, project_id="", tool="", idx=None) == raw
    )
