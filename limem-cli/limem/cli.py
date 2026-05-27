"""limem CLI 入口。

子命令：
- ping / info / bootstrap / update / doctor / hook / remember / init / stats
- daemon {start|stop|status|tail|reset}                            （阶段 1）
- statusline / sync-static / migrate {clean-static|undo-project-init} （阶段 2）
- export                                                            （阶段 5）
- dash [--logs] [--reset-suggestions]                              （阶段 7）
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .bootstrap import (
    MultipleDatabasesError,
    _db_id_of,
    _db_name_of,
    _normalize_db_listing,
    bootstrap_user_session,
)
from .client import LimemClient, LimemError
from .config import DEFAULT_BASE_URL, USER_CREDENTIALS_PATH, Credentials

console = Console()


def _daemon_health(*, spawn: bool = False) -> dict[str, object]:
    from . import daemon_client as dc
    from .config import LIMEMD_PID_PATH
    from .daemon.lock import read_pid

    if spawn:
        try:
            dc.ensure_or_spawn(max_wait_ms=300)
        except Exception:
            pass
    pid = read_pid(LIMEMD_PID_PATH)
    info = dc.get_status()
    return {
        "running": bool(info),
        "pid": pid,
        "status": info or {},
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def main() -> None:
    """LiMem CLI — claude-code / codex 接入辅助。"""


# ---------- ping / info / bootstrap ----------


@main.command()
def ping() -> None:
    """ping LiMem 后端，并检查本地 daemon 健康。"""
    creds = Credentials.load()
    if not creds.api_key:
        console.print(
            "[red]未找到 API Key[/red]。先运行 `limem bootstrap --api-key <YOUR_TOKEN>` "
            "或设置 LIMEM_API_KEY。"
        )
        sys.exit(2)
    c = LimemClient(creds=creds)
    try:
        me = c.me()
        console.print(f"[green]/me ok[/green]: {me}")
        if creds.db_id:
            h = c.db_health()
            console.print(f"[green]/db/{creds.db_id}/health ok[/green]: {h}")
        else:
            console.print(
                "[yellow]db_id 未配置。先跑 `limem bootstrap` 完成 db 解析。[/yellow]"
            )
        dbs = c.list_databases()
        console.print(f"[green]databases[/green]: {dbs}")
    except LimemError as e:
        console.print(f"[red]LiMem error {e.status}[/red]: {e.message}")
        sys.exit(1)
    daemon = _daemon_health(spawn=True)
    pid = daemon.get("pid") or "(none)"
    if daemon.get("running"):
        console.print(f"[green]limemd ok[/green]: pid={pid}")
    else:
        console.print(
            f"[yellow]limemd not running[/yellow]: pid={pid}; "
            "recall notices and passive learning are degraded. Run `limem daemon start`."
        )


@main.command()
def info() -> None:
    """显示当前凭证（脱敏 api_key）。"""
    creds = Credentials.load()
    masked = (creds.api_key[:6] + "..." + creds.api_key[-4:]) if creds.api_key else "(missing)"
    t = Table(show_header=False, box=None)
    t.add_row("base_url", creds.base_url)
    t.add_row("api_key", masked)
    t.add_row("db_id", creds.db_id or "(missing)")
    t.add_row("user_id", creds.user_id or "(missing)")
    t.add_row("credentials_path", str(USER_CREDENTIALS_PATH))
    console.print(t)


def _mask_key(key: str) -> str:
    if not key:
        return "(missing)"
    if len(key) <= 10:
        return "***"
    return key[:6] + "..." + key[-4:]


def _interactive_db_picker(dbs: list[dict]) -> int:
    """TTY 多 db 选择器；返回 dbs 列表中的索引。"""
    t = Table(title="你已有多个 db，请选择一个作为 active")
    t.add_column("#", justify="right")
    t.add_column("db_id")
    t.add_column("display_name")
    for i, db in enumerate(dbs):
        t.add_row(str(i), _db_id_of(db) or "?", _db_name_of(db))
    console.print(t)
    return click.prompt("Enter index", type=int)


@main.command()
@click.option(
    "--api-key",
    default="",
    envvar="LIMEM_API_KEY",
    help="LiMem user API key（从 dashboard 复制）；不传则从 stdin 或交互式 prompt 读取",
)
@click.option("--base-url", default=DEFAULT_BASE_URL, envvar="LIMEM_BASE_URL")
@click.option(
    "--db-name",
    default="claude-code-personal",
    help="当你尚无 db 时，自动创建的 display_name",
)
@click.option(
    "--select-db",
    default="",
    help="已有多 db 时非交互式指定 DB_ID",
)
@click.option("--save/--no-save", default=True)
def bootstrap(api_key: str, base_url: str, db_name: str, select_db: str, save: bool) -> None:
    """用你的 LiMem API key 接入 Claude Code / Codex；自动解析或创建 db。"""
    # 1. 解析 api_key：CLI 选项 / 环境变量 > stdin > 交互式 prompt
    token = (api_key or "").strip()
    if not token:
        if not sys.stdin.isatty():
            token = sys.stdin.read().strip()
        else:
            token = click.prompt("Enter your LiMem API key", hide_input=True).strip()
    if not token:
        console.print("[red]API key is required.[/red] 从 LiMem dashboard 复制后重试。")
        sys.exit(2)

    console.print(f"[bold]bootstrapping at {base_url}[/bold]")

    # 2. 多 db picker：TTY 才提供；非 TTY 直接抛 MultipleDatabasesError 让用户用 --select-db
    picker = _interactive_db_picker if sys.stdout.isatty() else None

    try:
        result = bootstrap_user_session(
            base_url=base_url,
            api_key=token,
            db_name=db_name,
            select_db_id=select_db or None,
            picker=picker,
        )
    except MultipleDatabasesError as e:
        console.print(
            f"[red]检测到 {len(e.dbs)} 个 db；非交互式环境请用 --select-db DB_ID 指定。[/red]"
        )
        for db in e.dbs:
            console.print(f"  - {_db_id_of(db)}  {_db_name_of(db)}")
        sys.exit(2)
    except LimemError as e:
        console.print(f"[red]bootstrap failed at LiMem {e.status}[/red]: {e.message}")
        if e.body:
            console.print(json.dumps(e.body, indent=2, ensure_ascii=False))
        if e.status in (401, 403):
            console.print(
                "[yellow]提示：请确认 API key 是从 LiMem dashboard 复制的、未过期。[/yellow]"
            )
        sys.exit(1)

    action_label = {
        "reused": "复用已有 db",
        "created": "已为你创建首个 db",
        "selected": "已选择 db",
    }.get(result.db_action, result.db_action)

    t = Table(title=f"Bootstrap OK — {action_label}", show_header=False)
    t.add_row("base_url", base_url)
    t.add_row("api_key", _mask_key(result.api_key))
    t.add_row("db_id", result.db_id)
    t.add_row("db_name", result.db_display_name or "(no name)")
    if result.user_id:
        t.add_row("user_id", result.user_id)
    console.print(t)

    if save:
        creds = Credentials(
            base_url=base_url,
            api_key=result.api_key,
            db_id=result.db_id,
            user_id=result.user_id,
        )
        creds.save()
        console.print(f"[green]saved to {USER_CREDENTIALS_PATH} (chmod 600)[/green]")

        # v3：bootstrap 成功后注册 user principal（失败静默）
        try:
            from .entity_index import EntityIndex
            from .principals import PrincipalSpec, register_principal

            if result.user_id:
                idx = EntityIndex()
                spec = PrincipalSpec(
                    principal_type="user",
                    slug=result.user_id,
                    description=f"当前 LiMem 账号用户：{result.user_id}",
                    aliases=["我", "用户", "the user", "myself", result.user_id],
                    scope="global",
                    canonical=f"user:{result.user_id}",
                )
                eid = register_principal(spec, creds=creds, idx=idx, swallow=True)
                console.print(f"[green]principal user → {eid}[/green]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]principal user 注册失败（已忽略）：{e}[/yellow]")
    else:
        console.print("[yellow]--no-save：未写入凭证文件[/yellow]")


# ---------- db 管理 ----------


def _require_api_key(creds: Credentials) -> None:
    if not creds.api_key:
        console.print(
            "[red]未配置凭证。先运行 `limem bootstrap --api-key <YOUR_TOKEN>`。[/red]"
        )
        sys.exit(2)


# ---------- update / doctor ----------


@main.command()
@click.option("--ref", default="main", help="git 分支或 tag（默认：main）")
@click.option("--target", "--targets", "targets", default="", help="auto / claude-code / codex / both")
@click.option("--no-init", is_flag=True, help="只更新 CLI，不刷新 hooks / MCP / skills")
@click.option("--bootstrap", is_flag=True, help="更新后强制运行 limem bootstrap")
@click.option("--no-bootstrap", is_flag=True, help="更新后跳过 limem bootstrap")
@click.option("--dry-run", is_flag=True, help="只显示安装计划，不实际更新")
@click.option("--verbose", "-v", is_flag=True, help="打印安装器调试输出")
def update(
    ref: str,
    targets: str,
    no_init: bool,
    bootstrap: bool,
    no_bootstrap: bool,
    dry_run: bool,
    verbose: bool,
) -> None:
    """从 GitHub 拉取最新安装器并更新 LiMem CLI / hooks / skills。"""
    if bootstrap and no_bootstrap:
        console.print("[red]--bootstrap 与 --no-bootstrap 不能同时使用[/red]")
        sys.exit(2)

    installer_url = (
        "https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/"
        f"{ref}/install.sh"
    )
    cmd = ["bash", "-c", "curl -fsSL \"$1\" | bash -s -- \"${@:2}\"", "limem-update", installer_url]
    cmd.extend(["--ref", ref, "--update"])
    if targets:
        cmd.extend(["--targets", targets])
    if no_init:
        cmd.append("--no-init")
    if bootstrap:
        cmd.append("--bootstrap")
    if no_bootstrap:
        cmd.append("--no-bootstrap")
    if dry_run:
        cmd.append("--dry-run")
    if verbose:
        cmd.append("--verbose")

    console.print(f"[bold]running installer[/bold]: {installer_url}")
    try:
        completed = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print("[red]找不到 bash 或 curl。请安装基础 shell 工具后重试。[/red]")
        sys.exit(13)
    sys.exit(completed.returncode)


@main.command()
@click.option("--fix", is_flag=True, help="自动修复 hooks / MCP / skills / PATH 链接，并尝试启动 daemon")
@click.option("--no-backend", is_flag=True, help="跳过后端连通性检查")
def doctor(fix: bool, no_backend: bool) -> None:
    """自动诊断 LiMem 安装、配置、daemon、凭证与项目初始化状态。"""
    from .doctor import run_doctor

    report = run_doctor(fix=fix, backend=not no_backend)
    t = Table(title="LiMem doctor")
    t.add_column("check")
    t.add_column("status")
    t.add_column("detail")
    t.add_column("fix")
    colors = {
        "ok": "green",
        "fixed": "green",
        "warn": "yellow",
        "error": "red",
        "skip": "dim",
    }
    for check in report.checks:
        style = colors.get(check.status, "white")
        t.add_row(
            check.name,
            f"[{style}]{check.status}[/{style}]",
            check.detail,
            check.fix,
        )
    console.print(t)
    if report.fixes:
        console.print("[bold]fixes applied[/bold]")
        for note in report.fixes:
            console.print(f"· {note}")
    if report.has_errors:
        sys.exit(1)
    if report.has_warnings:
        sys.exit(2)


@main.group()
def db() -> None:
    """db 管理：list / use / new。"""


@db.command("list")
def db_list() -> None:
    """列出当前 user 拥有的所有 db；标注当前 active。"""
    creds = Credentials.load()
    _require_api_key(creds)
    c = LimemClient(creds=creds)
    try:
        resp = c.list_databases()
    except LimemError as e:
        console.print(f"[red]list failed: {e.status} {e.message}[/red]")
        sys.exit(1)
    dbs = _normalize_db_listing(resp)
    if not dbs:
        console.print(
            "[yellow]还没有 db。先跑 `limem db new <NAME>` 或 `limem bootstrap`。[/yellow]"
        )
        return
    t = Table(title="Your databases")
    t.add_column("active", justify="center")
    t.add_column("db_id")
    t.add_column("display_name")
    for d in dbs:
        did = _db_id_of(d)
        active = "✓" if did and did == creds.db_id else ""
        t.add_row(active, did or "?", _db_name_of(d))
    console.print(t)


@db.command("use")
@click.argument("db_id")
def db_use(db_id: str) -> None:
    """把指定 DB_ID 设为 active（先校验归属，再写 credentials.json）。"""
    creds = Credentials.load()
    _require_api_key(creds)
    c = LimemClient(creds=creds)
    try:
        resp = c.list_databases()
    except LimemError as e:
        console.print(f"[red]list failed: {e.status} {e.message}[/red]")
        sys.exit(1)
    dbs = _normalize_db_listing(resp)
    matched = next((d for d in dbs if _db_id_of(d) == db_id), None)
    if matched is None:
        console.print(f"[red]db_id {db_id} 不在你的 db 列表里。[/red]")
        sys.exit(2)
    creds.db_id = db_id
    creds.save()
    console.print(
        f"[green]active db → {db_id} ({_db_name_of(matched) or 'no name'})[/green]"
    )


@db.command("new")
@click.argument("name")
@click.option("--use", "use_flag", is_flag=True, help="创建后立即切换为 active")
def db_new(name: str, use_flag: bool) -> None:
    """创建新 db；可选 --use 同时切换为 active。"""
    creds = Credentials.load()
    _require_api_key(creds)
    c = LimemClient(creds=creds)
    try:
        resp = c.create_database(display_name=name)
    except LimemError as e:
        console.print(f"[red]create failed: {e.status} {e.message}[/red]")
        sys.exit(1)
    new_id = _db_id_of(resp) if isinstance(resp, dict) else ""
    if not new_id:
        console.print(f"[red]后端未返回 db_id：{resp}[/red]")
        sys.exit(1)
    console.print(f"[green]created db {new_id} ({name})[/green]")
    if use_flag:
        creds.db_id = new_id
        creds.save()
        console.print(f"[green]active db → {new_id}[/green]")


# ---------- hook 调度 ----------


@main.command()
@click.argument("tool", type=click.Choice(["claude-code", "codex"]))
@click.argument(
    "event",
    type=click.Choice(
        [
            "UserPromptSubmit",
            "SessionStart",
            "SessionEnd",
            "Stop",
            "PreCompact",
            "PreToolUse",
            "PostToolUse",
        ]
    ),
)
def hook(tool: str, event: str) -> None:
    """Hook 调度入口；stdin 读 JSON，stdout 写 additionalContext JSON。"""
    from .hooks import main as hook_main
    sys.exit(hook_main([tool, event]))


# ---------- remember CLI ----------


@main.command()
@click.argument("text")
@click.option("--scope", default="", help="形如 project:owner/repo 或 global；默认按 cwd 检测")
@click.option("--type", "mem_type", default="rule")
@click.option("--importance", default=0.9, type=float)
@click.option(
    "--entity",
    "entities",
    multiple=True,
    help="mention：canonical 或 canonical|role 或 canonical|role|alias1,alias2；可重复",
)
def remember(text: str, scope: str, mem_type: str, importance: float, entities: tuple[str, ...]) -> None:
    """快速写入一条记忆（CLI 调试用）。

    v3：``--entity`` 描述一个 mention，**不会注册后端 entity**。第三段是 mention 别名。
    若要为 user / agent / project 沉淀长期档案，用 ``limem pattern put <alias>``。
    """
    from .memory_writer import EntitySpec
    from .memory_writer import remember as do_remember
    from .scope import detect_project_id

    project_id = detect_project_id()
    effective_scope = scope or f"project:{project_id}"
    ents: list[EntitySpec] = []
    for raw in entities:
        parts = raw.split("|", 2)
        canonical = parts[0].strip()
        if not canonical:
            console.print(f"[red]entity 格式错误[/red]: {raw}")
            sys.exit(2)
        role = parts[1].strip() if len(parts) > 1 else "neutral"
        aliases: list[str] = []
        if len(parts) > 2:
            aliases = [a.strip() for a in parts[2].split(",") if a.strip()]
        ents.append(EntitySpec(canonical=canonical, role=role, aliases=aliases))

    try:
        res = do_remember(
            text=text, scope=effective_scope, mem_type=mem_type,
            importance=importance, project_id=project_id,
            entities=ents or None, source="limem-cli:remember",
        )
    except (LimemError, ValueError) as e:
        console.print(f"[red]remember failed[/red]: {e}")
        sys.exit(1)

    console.print(
        f"[green]✓ saved[/green] event_id={res.event_id} "
        f"principals={len(res.principal_ids)} mentions={len(res.canonicals)}"
    )
    console.print(f"  scope={res.scope}")
    if res.principal_ids:
        console.print(f"  principal_ids: {', '.join(res.principal_ids)}")
    if res.canonicals:
        console.print(f"  canonicals: {', '.join(res.canonicals)}")
    console.print(f"  summary={res.summary}")


# ---------- init ----------


def _resolve_init_project_id(project_id: str, project_root: Path | None = None) -> str:
    if project_id.strip():
        return project_id.strip()

    from .installer import project_init_state

    state = project_init_state(project_root)
    if state.existing_project_id:
        return ""
    if not _init_is_interactive():
        return ""

    suggested = ""
    try:
        from .scope import detect_project_id

        suggested = detect_project_id(state.project_root)
    except Exception:
        suggested = ""

    prompt = "Project id"
    if suggested:
        prompt += f" [Enter 使用 {suggested}]"
    value = click.prompt(prompt, default="", show_default=False).strip()
    return value


def _init_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ensure_init_principals(project_id: str, *, tool: str = "") -> list[str]:
    """Best-effort ensure for principals after `limem init` writes project config."""
    if not project_id:
        return []
    try:
        from .client import LimemClient
        from .entity_index import EntityIndex
        from .principals import ensure_default_principals

        creds = Credentials.load()
        if not (creds.api_key and creds.db_id):
            return []
        client = LimemClient(creds=creds, timeout=2.0)
        return ensure_default_principals(
            creds,
            project_id=project_id,
            tool=tool,
            idx=EntityIndex(),
            client=client,
            include_user=True,
            include_agent=bool(tool),
            include_project=True,
        )
    except Exception:
        return []


@main.command()
@click.option("--project", is_flag=True, help="项目级 init（仅写 .limem/local.json + .gitignore）")
@click.option("--project-id", default="", help="首次项目级 init 时写入的稳定项目 id")
@click.option("--targets", default="")
@click.option("--no-hooks", is_flag=True)
@click.option("--no-mcp", is_flag=True)
@click.option("--no-skills", is_flag=True)
@click.option("--no-statusline", is_flag=True)
def init(project: bool, project_id: str, targets: str, no_hooks: bool, no_mcp: bool,
         no_skills: bool, no_statusline: bool) -> None:
    """安装 LiMem 到 Claude Code 和/或 Codex。"""
    from .installer import detect_targets, install_all, project_init

    if project:
        resolved_project_id = _resolve_init_project_id(project_id)
        plan = project_init(project_id=resolved_project_id)
        t = Table(title=f"Project init ({plan.project_id})", show_header=False)
        t.add_row("project_id", plan.project_id)
        t.add_row("root", str(plan.project_root))
        for note in plan.notes:
            t.add_row("·", note)
        console.print(t)
        if not (plan.local_json_written or plan.gitignore_patched):
            console.print("[yellow](no changes — already initialized)[/yellow]")

        # v3：init 写出项目配置后立即幂等 ensure user/project principals。
        # agent observer 仍由真实 SessionStart hook 按 tool 注册，避免 init 猜测执行主体。
        eids = _ensure_init_principals(plan.project_id)
        proj_eid = next((e for e in eids if e.startswith("principal_project_")), "")
        if proj_eid:
            console.print(f"[green]principal project → {proj_eid}[/green]")
        return

    target_list = [t.strip() for t in targets.split(",") if t.strip()] or detect_targets()
    console.print(f"[bold]targets:[/bold] {', '.join(target_list)}")
    resolved_project_id = _resolve_init_project_id(project_id)
    project_plan = project_init(project_id=resolved_project_id)
    plan = install_all(
        targets=target_list,
        enable_hooks=not no_hooks,
        enable_mcp=not no_mcp,
        enable_skills=not no_skills,
        enable_statusline=not no_statusline,
    )
    t = Table(title="LiMem install", show_header=False)
    t.add_row("claude-code patched", "yes" if plan.claude_settings_patched else "no")
    t.add_row("claude-code skills", str(plan.claude_skills_copied))
    t.add_row("codex patched", "yes" if plan.codex_config_patched else "no")
    t.add_row("codex skills", str(plan.codex_skills_copied))
    t.add_row("statusline", "yes" if plan.statusline_installed else "no")
    t.add_row("project_id", project_plan.project_id)
    t.add_row("project root", str(project_plan.project_root))
    if not (project_plan.local_json_written or project_plan.gitignore_patched):
        t.add_row("project init", "already initialized")
    for note in project_plan.notes:
        t.add_row("project ·", note)
    for note in plan.notes:
        t.add_row("·", note)
    console.print(t)
    eids = _ensure_init_principals(project_plan.project_id)
    proj_eid = next((e for e in eids if e.startswith("principal_project_")), "")
    if proj_eid:
        console.print(f"[green]principal project → {proj_eid}[/green]")
    creds = Credentials.load()
    if not creds.api_key:
        console.print(
            "[yellow]凭证尚未就绪。请运行 `limem bootstrap --api-key <YOUR_TOKEN>` "
            "或手动写 ~/.config/limem/credentials.json[/yellow]"
        )
    elif not creds.db_id:
        console.print(
            "[yellow]凭证 api_key 已就绪但缺 db_id。"
            "运行 `limem bootstrap --api-key <YOUR_TOKEN>` 或 `limem db list` 选一个。[/yellow]"
        )


# ---------- stats ----------


@main.command()
def stats() -> None:
    """本地 SQLite 缓存统计 + daemon/backend stats（若可达）。"""
    from .entity_index import EntityIndex
    idx = EntityIndex()
    s = idx.stats()
    t = Table(show_header=False, box=None)
    t.add_row("[bold]local[/bold]", "")
    for k, v in s.items():
        t.add_row(f"  {k}", str(v))

    daemon = _daemon_health(spawn=False)
    t.add_row("[bold]daemon[/bold]", "")
    t.add_row("  running", "yes" if daemon.get("running") else "no")
    t.add_row("  pid", str(daemon.get("pid") or "(none)"))
    status = daemon.get("status")
    if isinstance(status, dict) and status:
        t.add_row("  connectivity", str((status.get("connectivity") or {}).get("state", "unknown")))
        t.add_row("  hit_count", str(status.get("hit_count", 0)))
        t.add_row("  suggestion_count", str(status.get("suggestion_count", 0)))
    else:
        t.add_row("  hint", "run `limem daemon start`")

    creds = Credentials.load()
    if creds.api_key and creds.db_id:
        try:
            backend = LimemClient(creds=creds).db_stats()
            t.add_row("[bold]backend[/bold]", "")
            for k in (
                "episode_count",
                "event_count",
                "entity_count",
                "context_count",
                "involves_count",
            ):
                if k in backend:
                    t.add_row(f"  {k}", str(backend[k]))
        except LimemError as e:
            t.add_row("backend", f"[yellow]unreachable: {e.status}[/yellow]")
        except Exception as e:  # noqa: BLE001
            t.add_row("backend", f"[yellow]error: {e}[/yellow]")
    console.print(t)


# ---------- evolve / pattern ----------


@main.command()
def evolve() -> None:
    """触发后端 /evolve（衰减 + 归档）。"""
    creds = Credentials.load()
    if not creds.api_key or not creds.db_id:
        console.print("[red]缺凭证/db_id；先运行 `limem bootstrap`。[/red]")
        sys.exit(2)
    try:
        out = LimemClient(creds=creds).evolve()
        console.print(f"[green]evolve ok[/green]: {out}")
    except LimemError as e:
        console.print(f"[red]evolve failed[/red]: {e}")
        sys.exit(1)


def _resolve_principal_cli(alias_or_id: str) -> str:
    """CLI 内的 alias → entity_id 解析。"""
    from .entity_index import EntityIndex
    from .principals import principal_alias_to_id
    from .scope import detect_project_id

    creds = Credentials.load()
    project_id = detect_project_id()
    idx = EntityIndex()
    return principal_alias_to_id(
        alias_or_id, creds=creds, project_id=project_id, tool="claude-code", idx=idx
    )


@main.group()
def pattern() -> None:
    """principal markdown 档案 CRUD。

    第一个参数支持 alias：``project`` / ``user`` / ``agent`` 或 stable entity_id。
    """


@pattern.command("get")
@click.argument("entity_id")
def pattern_get_cmd(entity_id: str) -> None:
    creds = Credentials.load()
    if not creds.api_key or not creds.db_id:
        console.print("[red]缺凭证/db_id[/red]")
        sys.exit(2)
    eid = _resolve_principal_cli(entity_id)
    try:
        res = LimemClient(creds=creds).patterns_get(eid)
    except LimemError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    if not res.has_content():
        console.print(f"[yellow]principal {eid} 暂无档案[/yellow]")
        return
    console.print(f"[bold]principal:[/bold] {eid}  ({res.total_chars} chars)")
    console.print(res.content)


@pattern.command("put")
@click.argument("entity_id")
@click.option(
    "--file", "-f", "file_path",
    type=click.Path(exists=True),
    help="从文件读取 markdown（覆盖 stdin）",
)
def pattern_put_cmd(entity_id: str, file_path: str | None) -> None:
    """整篇 upsert principal markdown。从 --file 或 stdin 读取内容。"""
    from pathlib import Path
    if file_path:
        content = Path(file_path).read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()
    if not content.strip():
        console.print("[red]content is blank; 提供 --file 或在 stdin 输入[/red]")
        sys.exit(2)
    creds = Credentials.load()
    eid = _resolve_principal_cli(entity_id)
    try:
        action, p = LimemClient(creds=creds).patterns_upsert(eid, content)
    except LimemError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(
        f"[green]{action}[/green] principal={eid} pattern_id={p.pattern_id if p else '-'} "
        f"chars={len(content)}"
    )


@pattern.command("delete")
@click.argument("entity_id")
@click.confirmation_option(prompt="确认硬删除 principal 档案？此操作不可撤销")
def pattern_delete_cmd(entity_id: str) -> None:
    creds = Credentials.load()
    eid = _resolve_principal_cli(entity_id)
    try:
        snap = LimemClient(creds=creds).patterns_delete(eid)
    except LimemError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[green]deleted[/green] principal={eid} previous_id={snap.pattern_id if snap else '-'}")


# ---------- v3：entity / principal 管理 ----------


@main.group()
def entity() -> None:
    """principal 管理：list / register / activate / deactivate / prune-legacy。"""


@entity.command("list")
@click.option("--all", "show_all", is_flag=True, help="包含未激活的 principals")
@click.option("--type", "principal_types", multiple=True, help="按类型过滤")
def entity_list_cmd(show_all: bool, principal_types: tuple[str, ...]) -> None:
    from .entity_index import EntityIndex
    from .principals import ensure_default_principals
    from .scope import detect_project_id

    idx = EntityIndex()
    creds = Credentials.load()
    ensured_user = ""
    if creds.api_key and creds.db_id:
        try:
            eids = ensure_default_principals(
                creds,
                project_id=detect_project_id(),
                tool="",
                idx=idx,
                client=LimemClient(creds=creds, timeout=2.0),
                include_user=True,
                include_agent=False,
                include_project=True,
            )
            ensured_user = next((eid for eid in eids if eid.startswith("principal_user_")), "")
        except Exception:
            ensured_user = ""
    rows = idx.list_principals(
        active_only=not show_all,
        principal_types=list(principal_types) or None,
    )
    if not rows:
        console.print("[dim]no principals registered yet[/dim]")
        return
    t = Table(title=f"Principals ({len(rows)})")
    t.add_column("type")
    t.add_column("entity_id")
    t.add_column("canonical")
    t.add_column("pattern", justify="center")
    t.add_column("active", justify="center")
    for r in rows:
        t.add_row(
            r.principal_type,
            r.entity_id,
            (r.canonical or r.slug)[:48],
            "✓" if r.has_pattern else "",
            "✓" if r.active else "·",
        )
    console.print(t)
    if ensured_user:
        console.print(f"[green]principal user ensured → {ensured_user}[/green]")


@entity.command("register")
@click.argument("principal_type", type=click.Choice(["user", "agent", "project", "team", "service"]))
@click.argument("slug")
@click.option("--description", default="", help="人类可读说明")
@click.option("--alias", "aliases", multiple=True, help="可重复；写到后端 entity.aliases")
@click.option("--scope", default="global")
def entity_register_cmd(
    principal_type: str, slug: str, description: str, aliases: tuple[str, ...], scope: str
) -> None:
    from .entity_index import EntityIndex
    from .principals import PrincipalSpec, ensure_current_user_principal, register_principal

    creds = Credentials.load()
    if not creds.api_key or not creds.db_id:
        console.print("[red]缺凭证/db_id[/red]")
        sys.exit(2)
    idx = EntityIndex()
    ensured_user = ""
    try:
        ensured_user = ensure_current_user_principal(creds, idx=idx)
    except Exception:
        ensured_user = ""
    spec = PrincipalSpec(
        principal_type=principal_type,  # type: ignore[arg-type]
        slug=slug,
        description=description or f"{principal_type}:{slug}",
        aliases=list(aliases),
        scope=scope,
        canonical=f"{principal_type}:{slug}",
    )
    try:
        eid = register_principal(spec, creds=creds, idx=idx, swallow=False)
    except LimemError as e:
        console.print(f"[red]register failed: {e}[/red]")
        sys.exit(1)
    if ensured_user:
        console.print(f"[green]principal user → {ensured_user}[/green]")
    console.print(f"[green]registered[/green] {eid}")


@entity.command("activate")
@click.argument("entity_id")
def entity_activate_cmd(entity_id: str) -> None:
    from .entity_index import EntityIndex

    idx = EntityIndex()
    eid = _resolve_principal_cli(entity_id)
    idx.activate_principal(eid)
    console.print(f"[green]activated[/green] {eid}")


@entity.command("deactivate")
@click.argument("entity_id")
def entity_deactivate_cmd(entity_id: str) -> None:
    from .entity_index import EntityIndex

    idx = EntityIndex()
    eid = _resolve_principal_cli(entity_id)
    idx.deactivate_principal(eid)
    console.print(f"[yellow]deactivated[/yellow] {eid}")


@entity.command("prune-legacy")
@click.option("--delete", is_flag=True, help="实际删除（默认仅 dry-run）")
@click.confirmation_option(
    prompt="确认要删除后端遗留 v2 dense entity？此操作不可撤销", default=False,
    help="--yes 跳过确认（仅 --delete 需要）",
)
def entity_prune_legacy_cmd(delete: bool) -> None:
    """列出/删除后端遗留 v2 dense entity（``entity_type != 'principal'`` 且含 linked_event_id）。"""
    creds = Credentials.load()
    if not creds.api_key or not creds.db_id:
        console.print("[red]缺凭证/db_id[/red]")
        sys.exit(2)
    client = LimemClient(creds=creds)
    try:
        resp = client.entity_list()
    except LimemError as e:
        console.print(f"[red]entity_list failed: {e}[/red]")
        sys.exit(1)

    entities_raw: list[dict] = []
    if isinstance(resp, dict):
        entities_raw = list(resp.get("entities") or [])
    elif isinstance(resp, list):
        entities_raw = resp

    legacy: list[dict] = []
    for ent in entities_raw:
        if not isinstance(ent, dict):
            continue
        etype = ent.get("entity_type") or ent.get("type") or ""
        meta = ent.get("metadata") or {}
        eid = ent.get("entity_id") or ent.get("id")
        if not eid:
            continue
        if etype == "principal":
            continue
        if str(eid).startswith("principal_"):
            continue
        if "linked_event_id" in meta or "limem_scope" in meta:
            legacy.append({"entity_id": eid, "entity_type": etype, "metadata": meta})

    t = Table(title=f"Legacy entities ({len(legacy)})")
    t.add_column("entity_id")
    t.add_column("entity_type")
    t.add_column("scope")
    for ent in legacy[:50]:
        t.add_row(
            str(ent["entity_id"]),
            ent["entity_type"] or "-",
            (ent["metadata"] or {}).get("limem_scope", "-"),
        )
    console.print(t)
    if len(legacy) > 50:
        console.print(f"[dim]... and {len(legacy) - 50} more[/dim]")
    if not legacy:
        console.print("[green]nothing to prune[/green]")
        return
    if not delete:
        console.print("[yellow]dry-run: 添加 --delete 实际删除[/yellow]")
        return
    deleted = 0
    for ent in legacy:
        try:
            client.entity_delete(str(ent["entity_id"]))
            deleted += 1
        except LimemError as e:
            console.print(f"[red]delete {ent['entity_id']} failed: {e}[/red]")
    console.print(f"[green]deleted {deleted}/{len(legacy)} legacy entities[/green]")


# ---------- project 管理 ----------


@main.group()
def project() -> None:
    """项目管理：列出已注册项目 principal。"""


@project.command("list")
@click.option("--all", "show_all", is_flag=True, help="包含未激活的项目")
def project_list_cmd(show_all: bool) -> None:
    from .entity_index import EntityIndex
    from .scope import detect_project_id

    idx = EntityIndex()
    current_project_id = detect_project_id()
    rows = idx.list_principals(
        active_only=not show_all,
        principal_types=["project"],
    )
    if not rows:
        console.print("[dim]no projects registered yet[/dim]")
        if current_project_id:
            console.print(f"[dim]current project_id: {current_project_id}[/dim]")
        return

    t = Table(title=f"Projects ({len(rows)})")
    t.add_column("current", justify="center")
    t.add_column("project_id", no_wrap=True)
    t.add_column("entity_id", no_wrap=True)
    t.add_column("pattern", justify="center")
    t.add_column("active", justify="center")
    for r in rows:
        pid = r.project_id or r.slug
        t.add_row(
            "✓" if pid == current_project_id else "",
            pid,
            r.entity_id,
            "✓" if r.has_pattern else "",
            "✓" if r.active else "·",
        )
    console.print(t)


# ---------- daemon ----------


@main.group()
def daemon() -> None:
    """limemd daemon 管理子命令。"""


@daemon.command("start")
@click.option("--foreground", is_flag=True, help="前台运行（默认 detach）")
def daemon_start(foreground: bool) -> None:
    from .daemon.server import run as daemon_run
    if foreground:
        sys.exit(daemon_run([]))
    else:
        sys.exit(daemon_run(["--detach"]))


@daemon.command("stop")
def daemon_stop() -> None:
    from . import daemon_client as dc
    from .config import LIMEMD_PID_PATH
    from .daemon.lock import read_pid

    if dc.shutdown() is not None:
        console.print("[green]daemon shutdown signaled[/green]")
        return
    pid = read_pid(LIMEMD_PID_PATH)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]sent SIGTERM to pid={pid}[/green]")
            return
        except ProcessLookupError:
            console.print(f"[yellow]pid {pid} not found[/yellow]")
    console.print("[yellow]daemon not running[/yellow]")


@daemon.command("status")
def daemon_status() -> None:
    from . import daemon_client as dc
    from .config import LIMEMD_PID_PATH
    from .daemon.lock import read_pid

    pid = read_pid(LIMEMD_PID_PATH)
    info = dc.get_status() or {}
    t = Table(show_header=False, box=None)
    t.add_row("pid", str(pid or "(none)"))
    if info:
        for k, v in info.items():
            t.add_row(k, str(v))
    else:
        t.add_row("daemon", "[red]unreachable[/red]")
    console.print(t)


@daemon.command("tail")
@click.option("--from-start", is_flag=True)
def daemon_tail(from_start: bool) -> None:
    """tail events.ndjson。"""
    from .dash.app import run_logs
    sys.exit(run_logs(tail=not from_start))


@daemon.command("reset")
@click.option("--cache", is_flag=True, help="清空 pause/degraded/statusline cache 等 daemon 状态")
def daemon_reset(cache: bool) -> None:
    from .config import (
        DEGRADED_SEEN_PATH,
        PAUSE_PATH,
        SESSION_MUTE_PATH,
        STATUSLINE_CACHE_PATH,
        SUGGESTIONS_PATH,
    )
    paths = [PAUSE_PATH, DEGRADED_SEEN_PATH, SESSION_MUTE_PATH, STATUSLINE_CACHE_PATH]
    if cache:
        paths.append(SUGGESTIONS_PATH)
    n = 0
    for p in paths:
        try:
            p.unlink()
            n += 1
        except FileNotFoundError:
            pass
    console.print(f"[green]removed {n} cache files[/green]")


# ---------- statusline ----------


@main.command()
def statusline() -> None:
    """输出单行 statusline 字符串。"""
    from .statusline import render
    print(render())


# ---------- sync-static / migrate ----------


@main.command("sync-static")
@click.option("--target", type=click.Path(), default="AGENTS.md")
@click.option("--scope", default="project")
def sync_static(target: str, scope: str) -> None:
    """显式将后端规则镜像到本地静态文件（占位块形式）。"""
    from pathlib import Path

    from .entity_index import EntityIndex
    from .migrate import sync_static as do_sync
    from .scope import detect_project_id

    project_id = detect_project_id()
    scopes = ["global", f"project:{project_id}"] if scope == "project" else ["global"]
    idx = EntityIndex()
    metas = idx.list_hard_recall(allowed_scopes=scopes, allowed_types=["rule", "feedback", "preference"])
    body_lines = ["<!-- This block is managed by LiMem (`limem sync-static`). -->"]
    for m in metas:
        raw = m.raw_metadata or {}
        text = (raw.get("original_text") or m.summary or "").strip().replace("\n", " ")
        try:
            short = idx.ensure_short_id(m.event_id)
        except Exception:
            short = m.event_id[:12]
        body_lines.append(f"- [{m.mem_type}] {text} (#{short})")
    do_sync(Path.cwd(), agents_md=Path(target), rules_text="\n".join(body_lines))
    console.print(f"[green]synced {len(metas)} rules to {target}[/green]")


@main.group()
def migrate() -> None:
    """一次性迁移工具（升级老项目 / 老 settings）。"""


@migrate.command("clean-static")
@click.option("--root", type=click.Path(exists=True), default=".")
def migrate_clean_static(root: str) -> None:
    """清理历史项目中 LiMem 占位块（AGENTS.md / CLAUDE.md 引用行）。"""
    from pathlib import Path

    from .migrate import clean_static
    res = clean_static(Path(root).resolve())
    for n in res.notes:
        console.print(f"· {n}")
    if not res.notes:
        console.print("[dim]nothing to clean[/dim]")


@migrate.command("undo-project-init")
@click.option("--root", type=click.Path(exists=True), default=".")
def migrate_undo_project_init(root: str) -> None:
    """移除 .limem/local.json 与 .gitignore 中的 .limem/local.json 行。"""
    from pathlib import Path
    r = Path(root).resolve()
    local = r / ".limem" / "local.json"
    notes: list[str] = []
    if local.exists():
        local.unlink()
        notes.append(f"removed {local}")
    g = r / ".gitignore"
    if g.exists():
        lines = g.read_text().splitlines()
        kept = [ln for ln in lines if ln.strip() != ".limem/local.json"]
        if len(kept) != len(lines):
            g.write_text("\n".join(kept) + "\n")
            notes.append(".gitignore: removed .limem/local.json line")
    for n in notes:
        console.print(f"· {n}")
    if not notes:
        console.print("[dim]nothing to undo[/dim]")


@migrate.command("add-post-tool-use")
def migrate_add_post_tool_use() -> None:
    """老用户的 ~/.claude/settings.json 升级：追加 PostToolUse hook。"""
    from .installer import patch_claude_settings
    changed, notes = patch_claude_settings()
    for n in notes:
        console.print(f"· {n}")
    if not changed:
        console.print("[dim]already up-to-date[/dim]")


# ---------- export ----------


@main.command()
@click.option("--format", "fmt", type=click.Choice(["json", "markdown"]), default="json")
@click.option("--output", type=click.Path(), default=None)
@click.option("--include-tombstoned", is_flag=True)
@click.option(
    "--no-fill-patterns",
    "no_fill_patterns",
    is_flag=True,
    help="不调后端 patterns_get 拉每个 entity 的 markdown 档案（更快但导出不全）",
)
def export(fmt: str, output: str | None, include_tombstoned: bool, no_fill_patterns: bool) -> None:
    """导出本地全部记忆到 JSON 或 Markdown（v2：含 events + entities + 档案）。"""
    from pathlib import Path

    from .exporter import export as do_export
    out = do_export(
        fmt=fmt,
        output=Path(output) if output else None,
        include_tombstoned=include_tombstoned,
        fill_patterns=not no_fill_patterns,
    )
    console.print(f"[green]exported → {out}[/green]")


# ---------- dash ----------


@main.command()
@click.option("--logs", is_flag=True, help="切换到 logs 视图（tail events.ndjson）")
@click.option("--from-start", is_flag=True, help="logs 模式从头读")
@click.option("--reset-suggestions", is_flag=True, help="清空 suggestions.json 后退出")
def dash(logs: bool, from_start: bool, reset_suggestions: bool) -> None:
    """TUI 面板：状态 + 候选审批 / logs viewer。"""
    from .dash.app import reset_suggestions as do_reset
    from .dash.app import run_dashboard, run_logs

    if reset_suggestions:
        sys.exit(do_reset())
    if logs:
        sys.exit(run_logs(tail=not from_start))
    sys.exit(run_dashboard())


if __name__ == "__main__":
    main()
