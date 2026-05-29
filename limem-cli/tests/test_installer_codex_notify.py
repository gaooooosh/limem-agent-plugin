"""Codex notify 安装：写入 / 链式备份 / idempotent / 还原。"""

from __future__ import annotations

import json

import tomllib

from limem import installer as inst


def _setup(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    sidecar = tmp_path / "codex_prev_notify.json"
    monkeypatch.setattr(inst, "CODEX_PREV_NOTIFY_PATH", sidecar)
    monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/bin/{name}")
    return config_path, sidecar


def test_patch_sets_limem_notify_on_empty_config(monkeypatch, tmp_path) -> None:
    config_path, sidecar = _setup(monkeypatch, tmp_path)
    changed, _notes = inst.patch_codex_config(config_path=config_path)
    assert changed
    data = tomllib.loads(config_path.read_text())
    assert data["notify"] == ["/usr/bin/limem", "notify-codex"]
    assert not sidecar.exists()  # 无用户原 notify → 不写 sidecar


def test_patch_saves_user_notify_to_sidecar(monkeypatch, tmp_path) -> None:
    config_path, sidecar = _setup(monkeypatch, tmp_path)
    config_path.write_text('notify = ["my-notifier", "--toast"]\n')
    changed, notes = inst.patch_codex_config(config_path=config_path)
    assert changed
    data = tomllib.loads(config_path.read_text())
    assert data["notify"] == ["/usr/bin/limem", "notify-codex"]
    assert json.loads(sidecar.read_text()) == ["my-notifier", "--toast"]
    assert any("sidecar" in n for n in notes)


def test_patch_notify_idempotent(monkeypatch, tmp_path) -> None:
    config_path, _sidecar = _setup(monkeypatch, tmp_path)
    inst.patch_codex_config(config_path=config_path)
    # 第二次：notify 已是 limem → 不应再视为变化（仅 notify 维度）
    data_before = config_path.read_text()
    changed, _notes = inst.patch_codex_config(config_path=config_path)
    # hooks/mcp 也已写过，整体 changed 应为 False
    assert changed is False
    assert config_path.read_text() == data_before


def test_restore_codex_notify_from_sidecar(monkeypatch, tmp_path) -> None:
    config_path, sidecar = _setup(monkeypatch, tmp_path)
    config_path.write_text('notify = ["my-notifier", "--toast"]\n')
    inst.patch_codex_config(config_path=config_path)
    assert sidecar.exists()

    changed, notes = inst.restore_codex_notify(config_path=config_path)
    assert changed
    data = tomllib.loads(config_path.read_text())
    assert data["notify"] == ["my-notifier", "--toast"]
    assert not sidecar.exists()
    assert any("restored" in n for n in notes)


def test_restore_codex_notify_removes_when_no_sidecar(monkeypatch, tmp_path) -> None:
    config_path, _sidecar = _setup(monkeypatch, tmp_path)
    inst.patch_codex_config(config_path=config_path)  # 无用户 notify → 无 sidecar
    changed, _notes = inst.restore_codex_notify(config_path=config_path)
    assert changed
    data = tomllib.loads(config_path.read_text())
    assert "notify" not in data


def test_restore_codex_notify_skips_unmanaged(monkeypatch, tmp_path) -> None:
    config_path, _sidecar = _setup(monkeypatch, tmp_path)
    config_path.write_text('notify = ["someone-else"]\n')
    changed, notes = inst.restore_codex_notify(config_path=config_path)
    assert changed is False
    assert any("not managed" in n for n in notes)
