"""项目身份识别（按降序优先级）：

1. ``.limem/local.json`` 显式 ``project_id``
2. ``git remote get-url origin`` 规范化为 ``host/owner/repo``
3. 包管理器：``package.json#name`` / ``pyproject.toml#project.name`` / ``Cargo.toml#package.name``
4. cwd basename + sha1(abs_path)[:8]
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import tomllib

from .config import ProjectConfig


def normalize_git_remote(url: str) -> str:
    """git@github.com:foo/bar.git → github.com/foo/bar；https://github.com/foo/bar.git → github.com/foo/bar"""
    url = url.strip()
    if not url:
        return ""
    # SCP form: git@host:owner/repo(.git)
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return f"{host}/{path.strip('/')}"
    # URL form
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@]+@)?([\w.-]+)(?::\d+)?/(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return f"{host}/{path.strip('/')}"
    return url


def _git_remote(cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return normalize_git_remote(out.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def git_root(cwd: Path | None = None) -> Path | None:
    """返回 cwd 所在 git worktree 根目录；不在 git 仓库时返回 None。"""
    root = (cwd or Path.cwd()).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _package_json_name(cwd: Path) -> str:
    p = cwd / "package.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("name", "") or ""
        except Exception:
            return ""
    return ""


def _pyproject_name(cwd: Path) -> str:
    p = cwd / "pyproject.toml"
    if p.exists():
        try:
            data = tomllib.loads(p.read_text())
            return (data.get("project") or {}).get("name", "") or ""
        except Exception:
            return ""
    return ""


def _cargo_name(cwd: Path) -> str:
    p = cwd / "Cargo.toml"
    if p.exists():
        try:
            data = tomllib.loads(p.read_text())
            return (data.get("package") or {}).get("name", "") or ""
        except Exception:
            return ""
    return ""


def _fallback(cwd: Path) -> str:
    h = hashlib.sha1(str(cwd.resolve()).encode()).hexdigest()[:8]
    return f"{cwd.name}-{h}"


def detect_project_id(cwd: Path | None = None) -> str:
    cwd = (cwd or Path.cwd()).resolve()
    cfg = ProjectConfig.discover(cwd)
    if cfg and cfg.project_id:
        return cfg.project_id
    for fn in (_git_remote, _package_json_name, _pyproject_name, _cargo_name):
        value = fn(cwd)
        if value:
            return value
    return _fallback(cwd)


def project_scope(cwd: Path | None = None) -> str:
    return f"project:{detect_project_id(cwd)}"
