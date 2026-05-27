"""Installation diagnostics and best-effort repair helpers for ``limem doctor``."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tomllib

from .config import LIMEMD_PID_PATH, USER_CREDENTIALS_PATH, Credentials

CheckStatus = Literal["ok", "warn", "error", "fixed", "skip"]


@dataclass
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warn" for c in self.checks)

    def add(self, name: str, status: CheckStatus, detail: str, fix: str = "") -> None:
        self.checks.append(DoctorCheck(name=name, status=status, detail=detail, fix=fix))


def _command_exists(name: str) -> str:
    return shutil.which(name) or ""


def _load_json(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, "missing"
    try:
        loaded = json.loads(path.read_text() or "{}")
    except Exception as e:  # noqa: BLE001
        return {}, f"invalid json: {e}"
    if not isinstance(loaded, dict):
        return {}, "not an object"
    return loaded, ""


def _claude_command_present(settings: dict, event: str, command: str) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            if any(isinstance(h, dict) and h.get("command") == command for h in nested):
                return True
        elif entry.get("command") == command:
            return True
    return False


def _codex_command_present(config: dict, event: str, command: str) -> bool:
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            if any(isinstance(h, dict) and h.get("command") == command for h in nested):
                return True
        elif entry.get("command") == command:
            return True
    return False


def _count_skill_dirs(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir() if (p / "SKILL.md").exists())


def _check_python(report: DoctorReport) -> None:
    major, minor = sys.version_info[:2]
    if major > 3 or (major == 3 and minor >= 10):
        report.add("python", "ok", f"{major}.{minor} ({sys.executable})")
    else:
        report.add("python", "error", f"{major}.{minor}", "Install Python >= 3.10")


def _check_commands(report: DoctorReport, *, fix: bool) -> None:
    from .installer import ensure_path_symlinks

    required = ("limem", "limem-mcp", "limemd", "limem-statusline")
    missing = [name for name in required if not _command_exists(name)]
    if missing and fix:
        notes = ensure_path_symlinks()
        report.fixes.extend(notes)
        missing = [name for name in required if not _command_exists(name)]
    if missing:
        report.add(
            "commands",
            "error",
            "missing: " + ", ".join(missing),
            "Run `limem update` or reinstall with install.sh",
        )
        return
    report.add("commands", "ok", ", ".join(f"{n}={_command_exists(n)}" for n in required))


def _check_credentials(report: DoctorReport) -> None:
    creds = Credentials.load()
    if not USER_CREDENTIALS_PATH.exists() and not os.environ.get("LIMEM_API_KEY"):
        report.add(
            "credentials",
            "error",
            f"not found at {USER_CREDENTIALS_PATH}",
            "Run `limem bootstrap --api-key <YOUR_TOKEN>`",
        )
        return
    missing = []
    if not creds.api_key:
        missing.append("api_key")
    if not creds.db_id:
        missing.append("db_id")
    if missing:
        report.add(
            "credentials",
            "error",
            "missing " + ", ".join(missing),
            "Run `limem bootstrap --api-key <YOUR_TOKEN>`",
        )
        return
    mode = ""
    try:
        mode = stat.filemode(USER_CREDENTIALS_PATH.stat().st_mode)
    except OSError:
        mode = "env"
    report.add("credentials", "ok", f"api_key/db_id present ({mode})")


def _check_backend(report: DoctorReport) -> None:
    creds = Credentials.load()
    if not (creds.api_key and creds.db_id):
        report.add("backend", "skip", "credentials incomplete")
        return
    try:
        from .client import LimemClient, LimemError

        client = LimemClient(creds=creds, timeout=5.0)
        client.me()
        client.db_health()
    except LimemError as e:
        report.add("backend", "error", f"LiMem {e.status}: {e.message}", "Check API key/db_id")
    except Exception as e:  # noqa: BLE001
        report.add("backend", "warn", str(e), "Check network and LIMEM_BASE_URL")
    else:
        report.add("backend", "ok", f"{creds.base_url} db={creds.db_id}")


def _check_daemon(report: DoctorReport, *, fix: bool) -> None:
    try:
        from . import daemon_client as dc
        from .daemon.lock import read_pid

        info = dc.get_status()
        if not info and fix:
            dc.ensure_or_spawn(max_wait_ms=800)
            info = dc.get_status()
        pid = read_pid(LIMEMD_PID_PATH)
    except Exception as e:  # noqa: BLE001
        report.add("daemon", "warn", str(e), "Run `limem daemon start`")
        return
    if info:
        report.add("daemon", "ok", f"running pid={pid or '(unknown)'}")
    elif fix:
        report.add("daemon", "warn", f"not running pid={pid or '(none)'}", "Run `limem daemon start`")
    else:
        report.add("daemon", "warn", f"not running pid={pid or '(none)'}", "Run `limem doctor --fix`")


def _check_claude(report: DoctorReport, *, fix: bool) -> None:
    from .installer import (
        CLAUDE_CONFIG_DIR,
        CLAUDE_SETTINGS_PATH,
        CLAUDE_SKILLS_DIR,
        copy_skills,
        patch_claude_settings,
    )

    if not CLAUDE_CONFIG_DIR.exists():
        report.add("claude-code", "skip", f"{CLAUDE_CONFIG_DIR} not found")
        return
    data, err = _load_json(CLAUDE_SETTINGS_PATH)
    if err.startswith("invalid json") or err == "not an object":
        report.add("claude-code", "error", f"{CLAUDE_SETTINGS_PATH}: {err}", "Fix JSON then rerun")
        return
    required = {
        "UserPromptSubmit": "limem hook claude-code UserPromptSubmit",
        "SessionStart": "limem hook claude-code SessionStart",
        "Stop": "limem hook claude-code Stop",
    }
    missing = [event for event, cmd in required.items() if not _claude_command_present(data, event, cmd)]
    if missing and fix:
        changed, notes = patch_claude_settings(settings_path=CLAUDE_SETTINGS_PATH)
        report.fixes.extend(f"claude-code: {n}" for n in notes)
        if changed:
            data, _ = _load_json(CLAUDE_SETTINGS_PATH)
        missing = [
            event for event, cmd in required.items() if not _claude_command_present(data, event, cmd)
        ]
    skill_count = _count_skill_dirs(CLAUDE_SKILLS_DIR)
    if skill_count == 0 and fix:
        skill_count = copy_skills("claude-code")
        report.fixes.append(f"claude-code: copied {skill_count} skills")
    if missing:
        report.add("claude-code", "error", "missing hooks: " + ", ".join(missing), "Run `limem init --targets claude-code`")
    elif skill_count == 0:
        report.add("claude-code", "warn", "hooks ok, skills missing", "Run `limem doctor --fix`")
    else:
        report.add("claude-code", "ok", f"hooks ok, skills={skill_count}")


def _check_codex(report: DoctorReport, *, fix: bool) -> None:
    from .installer import (
        CODEX_CONFIG_DIR,
        CODEX_CONFIG_PATH,
        CODEX_SKILLS_DIR,
        copy_skills,
        patch_codex_config,
    )

    if not CODEX_CONFIG_DIR.exists():
        report.add("codex", "skip", f"{CODEX_CONFIG_DIR} not found")
        return
    if CODEX_CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CODEX_CONFIG_PATH.read_text())
        except Exception as e:  # noqa: BLE001
            report.add("codex", "error", f"{CODEX_CONFIG_PATH}: {e}", "Fix TOML then rerun")
            return
    else:
        data = {}
    required = {
        "UserPromptSubmit": "limem hook codex UserPromptSubmit",
        "SessionStart": "limem hook codex SessionStart",
        "Stop": "limem hook codex Stop",
    }
    missing = [event for event, cmd in required.items() if not _codex_command_present(data, event, cmd)]
    mcp = data.get("mcp_servers")
    mcp_missing = not (isinstance(mcp, dict) and isinstance(mcp.get("limem"), dict))
    if (missing or mcp_missing) and fix:
        changed, notes = patch_codex_config(config_path=CODEX_CONFIG_PATH)
        report.fixes.extend(f"codex: {n}" for n in notes)
        if changed and CODEX_CONFIG_PATH.exists():
            data = tomllib.loads(CODEX_CONFIG_PATH.read_text())
        missing = [
            event for event, cmd in required.items() if not _codex_command_present(data, event, cmd)
        ]
        mcp = data.get("mcp_servers")
        mcp_missing = not (isinstance(mcp, dict) and isinstance(mcp.get("limem"), dict))
    skill_count = _count_skill_dirs(CODEX_SKILLS_DIR)
    if skill_count == 0 and fix:
        skill_count = copy_skills("codex")
        report.fixes.append(f"codex: copied {skill_count} skills")
    if missing or mcp_missing:
        problems = []
        if missing:
            problems.append("missing hooks: " + ", ".join(missing))
        if mcp_missing:
            problems.append("missing mcp_servers.limem")
        report.add("codex", "error", "; ".join(problems), "Run `limem init --targets codex`")
    elif skill_count == 0:
        report.add("codex", "warn", "hooks/MCP ok, skills missing", "Run `limem doctor --fix`")
    else:
        report.add("codex", "ok", f"hooks/MCP ok, skills={skill_count}")


def _check_project(report: DoctorReport, *, fix: bool) -> None:
    from .installer import project_init, project_init_state
    from .scope import detect_project_id

    state = project_init_state()
    if not state.local_path.exists():
        if fix:
            plan = project_init(project_root=state.project_root)
            report.fixes.extend(plan.notes)
            report.add("project", "fixed", f"initialized {plan.project_id} at {plan.project_root}")
        else:
            report.add("project", "warn", f"{state.local_path} missing", "Run `limem init --project`")
        return
    try:
        project_id = detect_project_id(state.project_root)
    except Exception as e:  # noqa: BLE001
        report.add("project", "error", str(e), "Check .limem/local.json")
        return
    if project_id:
        report.add("project", "ok", f"{project_id} ({state.project_root})")
    elif fix:
        plan = project_init(project_root=state.project_root)
        report.fixes.extend(plan.notes)
        report.add("project", "fixed", f"initialized {plan.project_id} at {plan.project_root}")
    else:
        report.add("project", "warn", f"{state.local_path} has no project_id", "Run `limem doctor --fix`")


def run_doctor(*, fix: bool = False, backend: bool = True) -> DoctorReport:
    report = DoctorReport()
    _check_python(report)
    _check_commands(report, fix=fix)
    _check_credentials(report)
    if backend:
        _check_backend(report)
    else:
        report.add("backend", "skip", "backend check disabled")
    _check_daemon(report, fix=fix)
    _check_claude(report, fix=fix)
    _check_codex(report, fix=fix)
    _check_project(report, fix=fix)
    return report
