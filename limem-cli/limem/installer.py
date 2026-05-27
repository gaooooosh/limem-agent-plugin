"""``limem init`` 安装器：把 hooks / skills / MCP server / statusline 部署到 Claude Code 与 Codex。

设计原则：
- 绝不覆盖用户已有 hooks / mcpServers / statusLine；必须合并，新增 ``limem*`` 命名段
- 凭证写到 ``~/.config/limem/credentials.json``，**绝不**进任一工具配置或项目目录
- 项目级 ``limem init --project`` 仅写 ``.limem/local.json`` 与 ``.gitignore``——
  阶段 2 起**不再**改 AGENTS.md / CLAUDE.md（如需镜像静态规则用 ``limem sync-static``）
- 所有修改都先备份 ``.limem-bak``
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
import tomllib

# ---------- 路径常量 ----------

CLAUDE_CONFIG_DIR = Path("~/.claude").expanduser()
CLAUDE_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "settings.json"
CLAUDE_SKILLS_DIR = CLAUDE_CONFIG_DIR / "skills"

CODEX_CONFIG_DIR = Path("~/.codex").expanduser()
CODEX_CONFIG_PATH = CODEX_CONFIG_DIR / "config.toml"
CODEX_SKILLS_DIR = Path("~/.agents/skills").expanduser()

USER_LOCAL_BIN = Path("~/.local/bin").expanduser()


# ---------- 数据 ----------


@dataclass
class InstallPlan:
    targets: list[str] = field(default_factory=list)
    claude_settings_patched: bool = False
    codex_config_patched: bool = False
    claude_skills_copied: int = 0
    codex_skills_copied: int = 0
    statusline_installed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ProjectInitPlan:
    project_id: str = ""
    project_root: Path = field(default_factory=Path)
    local_json_written: bool = False
    gitignore_patched: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ProjectInitState:
    project_root: Path
    local_path: Path
    existing_project_id: str = ""


# ---------- 工具探测 ----------


def detect_targets() -> list[str]:
    out: list[str] = []
    if CLAUDE_CONFIG_DIR.exists():
        out.append("claude-code")
    if CODEX_CONFIG_DIR.exists():
        out.append("codex")
    if not out:
        out = ["claude-code", "codex"]
    return out


def _bundled_skills_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "plugin-src" / "skills",
        here.parent / "skills",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    raise FileNotFoundError(f"bundled skills dir not found; looked in: {candidates}")


# ---------- Claude Code hooks（含 PostToolUse） ----------


_CLAUDE_HOOKS: dict[str, list[dict[str, Any]]] = {
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "limem hook claude-code UserPromptSubmit"}]}
    ],
    "SessionStart": [
        {
            "matcher": "startup|resume",
            "hooks": [{"type": "command", "command": "limem hook claude-code SessionStart"}],
        }
    ],
    "SessionEnd": [
        {"hooks": [{"type": "command", "command": "limem hook claude-code SessionEnd"}]}
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": "limem hook claude-code Stop"}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": "limem hook claude-code PreCompact"}]}
    ],
    "PreToolUse": [
        {
            "matcher": "Edit|Write|NotebookEdit|Bash",
            "hooks": [{"type": "command", "command": "limem hook claude-code PreToolUse"}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Edit|Write|NotebookEdit",
            "hooks": [{"type": "command", "command": "limem hook claude-code PostToolUse"}],
        }
    ],
}

_CLAUDE_MCP_SERVER = {"limem": {"command": "limem-mcp", "args": []}}

_CLAUDE_STATUSLINE = {"type": "command", "command": "limem-statusline", "padding": 0}

_CODEX_HOOKS: dict[str, list[dict[str, Any]]] = {
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "limem hook codex UserPromptSubmit"}]}
    ],
    "SessionStart": [
        {"hooks": [{"type": "command", "command": "limem hook codex SessionStart"}]}
    ],
    "Stop": [{"hooks": [{"type": "command", "command": "limem hook codex Stop"}]}],
}


def patch_claude_settings(
    settings_path: Path = CLAUDE_SETTINGS_PATH,
    *,
    enable_hooks: bool = True,
    enable_mcp: bool = True,
    enable_statusline: bool = True,
) -> tuple[bool, list[str]]:
    """合并 hooks + mcpServers + statusLine 到 ~/.claude/settings.json。"""
    notes: list[str] = []
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        data = json.loads(settings_path.read_text() or "{}")
    else:
        data = {}

    changed = False

    if enable_hooks:
        hooks = data.setdefault("hooks", {})
        for event, entries in _CLAUDE_HOOKS.items():
            existing = hooks.setdefault(event, [])
            for new_entry in entries:
                cmd = new_entry["hooks"][0]["command"]
                already = any(
                    (
                        any(h.get("command") == cmd for h in e.get("hooks", []))
                        if isinstance(e, dict) and "hooks" in e
                        else e.get("command") == cmd
                    )
                    for e in existing
                )
                if not already:
                    existing.append(new_entry)
                    changed = True
                    notes.append(f"hook +{event}")

    if enable_mcp:
        mcp = data.setdefault("mcpServers", {})
        for name, cfg in _CLAUDE_MCP_SERVER.items():
            if mcp.get(name) != cfg:
                mcp[name] = cfg
                changed = True
                notes.append(f"mcpServer +{name}")

    if enable_statusline:
        existing_sl = data.get("statusLine")
        # 用户已配置 → 不覆盖；记 note
        if existing_sl and isinstance(existing_sl, dict):
            existing_cmd = existing_sl.get("command", "")
            if existing_cmd and "limem" not in existing_cmd:
                notes.append("statusLine: user-defined, skipped")
            elif existing_sl != _CLAUDE_STATUSLINE:
                data["statusLine"] = _CLAUDE_STATUSLINE
                changed = True
                notes.append("statusLine: updated to limem statusline")
        else:
            data["statusLine"] = _CLAUDE_STATUSLINE
            changed = True
            notes.append("statusLine +limem statusline")

    if changed:
        if settings_path.exists():
            bak = settings_path.with_suffix(settings_path.suffix + ".limem-bak")
            shutil.copy2(settings_path, bak)
            notes.append(f"backup → {bak.name}")
        settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    return changed, notes


def patch_codex_config(
    config_path: Path = CODEX_CONFIG_PATH,
    *,
    enable_hooks: bool = True,
    enable_mcp: bool = True,
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        data = tomllib.loads(config_path.read_text())
    else:
        data = {}

    changed = False

    if enable_hooks:
        hooks_block = data.setdefault("hooks", {})
        for event, entries in _CODEX_HOOKS.items():
            existing_list = hooks_block.setdefault(event, [])
            if not isinstance(existing_list, list):
                continue
            for new_entry in entries:
                cmd = new_entry["hooks"][0]["command"]
                already = any(
                    (
                        any(h.get("command") == cmd for h in e.get("hooks", []))
                        if isinstance(e, dict) and "hooks" in e
                        else isinstance(e, dict) and e.get("command") == cmd
                    )
                    for e in existing_list
                )
                if not already:
                    existing_list.append(new_entry)
                    changed = True
                    notes.append(f"codex hook +{event}")

    if enable_mcp:
        mcp = data.setdefault("mcp_servers", {})
        if mcp.get("limem") != {"command": "limem-mcp", "args": []}:
            mcp["limem"] = {"command": "limem-mcp", "args": []}
            changed = True
            notes.append("codex mcp_servers +limem")

    if changed:
        if config_path.exists():
            bak = config_path.with_suffix(config_path.suffix + ".limem-bak")
            shutil.copy2(config_path, bak)
            notes.append(f"backup → {bak.name}")
        config_path.write_text(tomli_w.dumps(data))

    return changed, notes


# ---------- Skills 铺设 ----------


def copy_skills(target: str) -> int:
    src = _bundled_skills_dir()
    if target == "claude-code":
        dst_root = CLAUDE_SKILLS_DIR
    elif target == "codex":
        dst_root = CODEX_SKILLS_DIR
    else:
        raise ValueError(f"unknown target: {target}")
    dst_root.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill_dir in src.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        dst_dir = dst_root / skill_dir.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, dst_dir / "SKILL.md")
        count += 1
    return count


def ensure_path_symlinks() -> list[str]:
    import shutil as _sh
    import sys as _sys

    notes: list[str] = []
    bin_names = ("limem", "limem-mcp", "limem-statusline", "limemd")
    existing = {name: _sh.which(name) for name in bin_names}
    if all(existing.values()):
        notes.append(
            "`limem` 命令组已在 PATH: "
            + ", ".join(f"{name}={path}" for name, path in existing.items() if path)
        )
        return notes

    USER_LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
    candidates: list[Path] = []
    if venv_bin.exists():
        candidates.append(venv_bin)
    candidates.append(Path(_sys.executable).parent)
    for cand in candidates:
        if all((cand / binname).exists() for binname in bin_names):
            for binname in bin_names:
                src = cand / binname
                (USER_LOCAL_BIN / binname).unlink(missing_ok=True)
                (USER_LOCAL_BIN / binname).symlink_to(src)
            notes.append(f"symlinked binaries → {cand}")
            return notes
    notes.append(
        "WARNING: 未能定位 limem/limem-mcp 可执行文件；hook 可能找不到 limem 命令。"
    )
    return notes


def install_all(
    *,
    targets: list[str] | None = None,
    enable_hooks: bool = True,
    enable_mcp: bool = True,
    enable_skills: bool = True,
    enable_statusline: bool = True,
) -> InstallPlan:
    plan = InstallPlan(targets=targets or detect_targets())
    plan.notes.extend(f"PATH: {n}" for n in ensure_path_symlinks())
    for target in plan.targets:
        if target == "claude-code":
            changed, notes = patch_claude_settings(
                enable_hooks=enable_hooks,
                enable_mcp=enable_mcp,
                enable_statusline=enable_statusline,
            )
            plan.claude_settings_patched = changed
            plan.statusline_installed = plan.statusline_installed or any(
                "statusLine" in n for n in notes
            )
            plan.notes.extend(f"claude-code: {n}" for n in notes)
            if enable_skills:
                plan.claude_skills_copied = copy_skills("claude-code")
                plan.notes.append(f"claude-code: skills {plan.claude_skills_copied} 个")
        elif target == "codex":
            changed, notes = patch_codex_config(
                enable_hooks=enable_hooks, enable_mcp=enable_mcp
            )
            plan.codex_config_patched = changed
            plan.notes.extend(f"codex: {n}" for n in notes)
            if enable_skills:
                plan.codex_skills_copied = copy_skills("codex")
                plan.notes.append(f"codex: skills {plan.codex_skills_copied} 个")

    # v3：首次 install 时尝试注册 user / project 默认 principals（失败静默）
    try:
        from .config import Credentials
        from .entity_index import EntityIndex
        from .principals import ensure_default_principals
        from .scope import detect_project_id

        creds = Credentials.load()
        if creds.api_key and creds.db_id:
            ids = ensure_default_principals(
                creds,
                project_id=detect_project_id(),
                tool="",  # tool 由 SessionStart hook 时再补 agent principal
                idx=EntityIndex(),
            )
            if ids:
                plan.notes.append(f"principals ensured: {', '.join(ids)}")
    except Exception as e:  # noqa: BLE001
        plan.notes.append(f"principals ensure skipped: {e}")

    return plan


# ---------- 项目级 init（阶段 2/10：不再写 AGENTS.md/CLAUDE.md） ----------


def project_init_state(project_root: Path | None = None) -> ProjectInitState:
    from .scope import git_root

    requested_root = (project_root or Path.cwd()).resolve()
    root = git_root(requested_root) or requested_root
    local_path = root / ".limem" / "local.json"
    existing: dict[str, Any] = {}
    if local_path.exists():
        try:
            loaded = json.loads(local_path.read_text() or "{}")
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}
    return ProjectInitState(
        project_root=root,
        local_path=local_path,
        existing_project_id=str(existing.get("project_id") or "").strip(),
    )


def project_init(
    project_root: Path | None = None,
    *,
    project_id: str = "",
) -> ProjectInitPlan:
    from .scope import detect_project_id

    state = project_init_state(project_root)
    root = state.project_root
    local_dir = state.local_path.parent
    local_dir.mkdir(exist_ok=True)
    local_path = state.local_path

    existing: dict[str, Any] = {}
    if local_path.exists():
        try:
            loaded = json.loads(local_path.read_text() or "{}")
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}

    requested_pid = project_id.strip()
    pid = str(existing.get("project_id") or "").strip() or requested_pid or detect_project_id(root)
    plan = ProjectInitPlan(project_id=pid, project_root=root)

    payload = dict(existing)
    payload["project_id"] = pid
    payload.setdefault("enabled_hooks", [])
    if not local_path.exists() or payload != existing:
        local_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        plan.local_json_written = True
        plan.notes.append(f"wrote {local_path.relative_to(root)}")

    gitignore = root / ".gitignore"
    line = ".limem/local.json"
    if gitignore.exists():
        existing = gitignore.read_text()
        if line not in existing.splitlines():
            with gitignore.open("a") as f:
                if not existing.endswith("\n"):
                    f.write("\n")
                f.write(line + "\n")
            plan.gitignore_patched = True
            plan.notes.append(".gitignore 已追加 .limem/local.json")
    else:
        gitignore.write_text(line + "\n")
        plan.gitignore_patched = True
        plan.notes.append("创建了 .gitignore")

    return plan
