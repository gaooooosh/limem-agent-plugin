"""A2：`limem hook claude-code PreToolUse` 不被 Click choice 拒绝。

修复点位：limem-cli/limem/cli.py 的 hook 子命令 Click.Choice
"""

from __future__ import annotations

from click.testing import CliRunner

from limem.cli import main


def test_hook_pre_tool_use_accepted_by_click() -> None:
    """传 stdin 空 JSON，hook 应正常退出 0；如果 Click 拒绝会返回非 0 退出码。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "PreToolUse"], input="{}")
    # exit_code 不为 2（Click usage error）就说明 choice 接受了
    assert result.exit_code != 2, (
        f"Click 拒绝了 PreToolUse choice，输出:\n{result.output}"
    )


def test_hook_post_tool_use_still_accepted() -> None:
    """回归保护：原有 PostToolUse 仍可用。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "PostToolUse"], input="{}")
    assert result.exit_code != 2


def test_hook_unknown_event_rejected() -> None:
    """未知事件名仍应被 Click 拒绝（exit_code=2 是 Click usage error）。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "NonexistentEvent"], input="{}")
    assert result.exit_code == 2


def test_init_project_id_option_is_accepted(monkeypatch, tmp_path) -> None:
    """首次 project init 可显式写入稳定 project_id，重复 init 不覆盖旧值。"""
    import json

    import limem.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Credentials, "load", lambda: type("Creds", (), {"api_key": "", "db_id": ""})())

    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--project", "--project-id", "manual-project-id"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".limem" / "local.json").read_text())
    assert payload["project_id"] == "manual-project-id"

    result = runner.invoke(
        main, ["init", "--project", "--project-id", "different-project-id"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".limem" / "local.json").read_text())
    assert payload["project_id"] == "manual-project-id"


def test_interactive_project_init_accepts_project_id(monkeypatch, tmp_path) -> None:
    import json

    import limem.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_init_is_interactive", lambda: True)
    monkeypatch.setattr(cli.Credentials, "load", lambda: type("Creds", (), {"api_key": "", "db_id": ""})())

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project"], input="custom-project\n")

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".limem" / "local.json").read_text())
    assert payload["project_id"] == "custom-project"


def test_interactive_project_init_empty_input_uses_generated_id(monkeypatch, tmp_path) -> None:
    import json

    import limem.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_init_is_interactive", lambda: True)
    monkeypatch.setattr(cli.Credentials, "load", lambda: type("Creds", (), {"api_key": "", "db_id": ""})())

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--project"], input="\n")

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".limem" / "local.json").read_text())
    assert payload["project_id"]
    assert payload["project_id"] != "custom-project"


def test_default_init_creates_local_project_config(monkeypatch, tmp_path) -> None:
    """普通 limem init 也会为当前目录自动创建稳定 project_id。"""
    import json
    from types import SimpleNamespace

    import limem.cli as cli
    import limem.installer as installer

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(installer, "detect_targets", lambda: [])
    monkeypatch.setattr(
        installer,
        "install_all",
        lambda **_: SimpleNamespace(
            claude_settings_patched=False,
            claude_skills_copied=0,
            codex_config_patched=False,
            codex_skills_copied=0,
            statusline_installed=False,
            notes=[],
        ),
    )
    monkeypatch.setattr(cli.Credentials, "load", lambda: type("Creds", (), {"api_key": "", "db_id": ""})())

    runner = CliRunner()
    result = runner.invoke(main, ["init"])

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".limem" / "local.json").read_text())
    assert payload["project_id"]


def test_project_list_shows_registered_projects(monkeypatch, tmp_path) -> None:
    import limem.cli as cli
    import limem.entity_index as entity_index

    idx = entity_index.EntityIndex(db_path=tmp_path / "patterns.sqlite")
    idx.upsert_principal(
        entity_id="principal_project_deadbeef",
        principal_type="project",
        slug="github.com/foo/bar",
        canonical="project:bar",
        aliases=["bar"],
        description="测试项目",
        scope="project:github.com/foo/bar",
        project_id="github.com/foo/bar",
        has_pattern=True,
    )

    monkeypatch.setattr(cli, "EntityIndex", lambda: idx, raising=False)
    monkeypatch.setattr(entity_index, "EntityIndex", lambda: idx)
    monkeypatch.setattr("limem.scope.detect_project_id", lambda: "github.com/foo/bar")

    runner = CliRunner()
    result = runner.invoke(main, ["project", "list"])

    assert result.exit_code == 0, result.output
    assert "github.com/foo/bar" in result.output
    assert "principal_project_deadbeef" in result.output
