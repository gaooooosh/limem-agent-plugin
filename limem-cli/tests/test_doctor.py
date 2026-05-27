from __future__ import annotations

import json

from click.testing import CliRunner


def test_doctor_reports_offline_install_state(tmp_path, monkeypatch) -> None:
    import limem.config as cfg
    import limem.doctor as doc
    import limem.installer as inst

    home = tmp_path / "home"
    config_dir = home / ".config" / "limem"
    cache_dir = home / ".cache" / "limem"
    claude_dir = home / ".claude"
    codex_dir = home / ".codex"
    skills_dir = home / ".agents" / "skills"
    config_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    claude_dir.mkdir()
    codex_dir.mkdir()

    credentials = config_dir / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "base_url": "https://limem.example.test",
                "api_key": "sk-test",
                "db_id": "db_test",
                "user_id": "u_test",
            }
        )
    )
    monkeypatch.setattr(cfg, "USER_CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg, "USER_CREDENTIALS_PATH", credentials)
    monkeypatch.setattr(doc, "USER_CREDENTIALS_PATH", credentials)
    monkeypatch.setattr(cfg, "USER_CACHE_DIR", cache_dir)
    monkeypatch.setattr(cfg, "LIMEMD_PID_PATH", cache_dir / "limemd.pid")
    monkeypatch.setattr(doc, "LIMEMD_PID_PATH", cache_dir / "limemd.pid")
    monkeypatch.setattr(inst, "CLAUDE_CONFIG_DIR", claude_dir)
    monkeypatch.setattr(inst, "CLAUDE_SETTINGS_PATH", claude_dir / "settings.json")
    monkeypatch.setattr(inst, "CLAUDE_SKILLS_DIR", claude_dir / "skills")
    monkeypatch.setattr(inst, "CODEX_CONFIG_DIR", codex_dir)
    monkeypatch.setattr(inst, "CODEX_CONFIG_PATH", codex_dir / "config.toml")
    monkeypatch.setattr(inst, "CODEX_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(inst, "USER_LOCAL_BIN", home / ".local" / "bin")
    monkeypatch.setattr(doc.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("limem.daemon_client.get_status", lambda: None)
    monkeypatch.setattr("limem.daemon.lock.read_pid", lambda _path: None)
    monkeypatch.chdir(tmp_path)

    report = doc.run_doctor(fix=True, backend=False)
    by_name = {check.name: check for check in report.checks}

    assert by_name["credentials"].status == "ok"
    assert by_name["backend"].status == "skip"
    assert by_name["claude-code"].status == "ok"
    assert by_name["codex"].status == "ok"
    assert by_name["project"].status == "fixed"
    assert (claude_dir / "settings.json").exists()
    assert (codex_dir / "config.toml").exists()
    assert (tmp_path / ".limem" / "local.json").exists()


def test_cli_registers_update_and_doctor() -> None:
    from limem.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "update" in result.output
    assert "doctor" in result.output
