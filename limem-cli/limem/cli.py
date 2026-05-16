"""limem CLI 入口。

子命令：
- ping / info / bootstrap / hook / remember / init / stats         （阶段 0 既有）
- daemon {start|stop|status|tail|reset}                            （阶段 1）
- statusline / sync-static / migrate {clean-static|undo-project-init} （阶段 2）
- export                                                            （阶段 5）
- dash [--logs] [--reset-suggestions]                              （阶段 7）
"""

from __future__ import annotations

import json
import os
import signal
import sys

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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def main() -> None:
    """LiMem CLI — claude-code / codex 接入辅助。"""


# ---------- ping / info / bootstrap ----------


@main.command()
def ping() -> None:
    """ping LiMem 后端，验证 key 与 db_id 可用。"""
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
    else:
        console.print("[yellow]--no-save：未写入凭证文件[/yellow]")


# ---------- db 管理 ----------


def _require_api_key(creds: Credentials) -> None:
    if not creds.api_key:
        console.print(
            "[red]未配置凭证。先运行 `limem bootstrap --api-key <YOUR_TOKEN>`。[/red]"
        )
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
        ["UserPromptSubmit", "SessionStart", "SessionEnd", "Stop", "PreCompact", "PostToolUse"]
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
@click.option("--entity", "entities", multiple=True, help='canonical|role|p1,p2,p3；可重复')
def remember(text: str, scope: str, mem_type: str, importance: float, entities: tuple[str, ...]) -> None:
    """快速写入一条记忆（CLI 调试用）。"""
    from .memory_writer import EntitySpec
    from .memory_writer import remember as do_remember
    from .scope import detect_project_id

    project_id = detect_project_id()
    effective_scope = scope or f"project:{project_id}"
    ents: list[EntitySpec] = []
    for raw in entities:
        try:
            canonical, role, patterns_csv = raw.split("|", 2)
        except ValueError:
            console.print(f"[red]entity 格式错误[/red]: {raw}")
            sys.exit(2)
        patterns = [p.strip() for p in patterns_csv.split(",") if p.strip()]
        ents.append(EntitySpec(canonical=canonical.strip(), role=role.strip(), patterns=patterns))

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
        f"[green]✓ saved[/green] event_id={res.event_id} entities={len(res.entity_ids)} patterns={res.pattern_count}"
    )
    console.print(f"  scope={res.scope}")
    console.print(f"  summary={res.summary}")


# ---------- init ----------


@main.command()
@click.option("--project", is_flag=True, help="项目级 init（仅写 .limem/local.json + .gitignore）")
@click.option("--targets", default="")
@click.option("--no-hooks", is_flag=True)
@click.option("--no-mcp", is_flag=True)
@click.option("--no-skills", is_flag=True)
@click.option("--no-statusline", is_flag=True)
def init(project: bool, targets: str, no_hooks: bool, no_mcp: bool, no_skills: bool,
         no_statusline: bool) -> None:
    """安装 LiMem 到 Claude Code 和/或 Codex。"""
    from .installer import detect_targets, install_all, project_init

    if project:
        plan = project_init()
        t = Table(title=f"Project init ({plan.project_id})", show_header=False)
        t.add_row("project_id", plan.project_id)
        t.add_row("root", str(plan.project_root))
        for note in plan.notes:
            t.add_row("·", note)
        console.print(t)
        if not (plan.local_json_written or plan.gitignore_patched):
            console.print("[yellow](no changes — already initialized)[/yellow]")
        return

    target_list = [t.strip() for t in targets.split(",") if t.strip()] or detect_targets()
    console.print(f"[bold]targets:[/bold] {', '.join(target_list)}")
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
    for note in plan.notes:
        t.add_row("·", note)
    console.print(t)
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
    """本地 SQLite 缓存统计。"""
    from .pattern_index import PatternIndex
    pidx = PatternIndex()
    s = pidx.stats()
    t = Table(show_header=False, box=None)
    for k, v in s.items():
        t.add_row(k, str(v))
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

    from .migrate import sync_static as do_sync
    from .pattern_index import PatternIndex
    from .scope import detect_project_id

    project_id = detect_project_id()
    scopes = ["global", f"project:{project_id}"] if scope == "project" else ["global"]
    pidx = PatternIndex()
    metas = pidx.list_hard_recall(allowed_scopes=scopes, allowed_types=["rule", "feedback", "preference"])
    body_lines = ["<!-- This block is managed by LiMem (`limem sync-static`). -->"]
    for m in metas:
        raw = m.raw_metadata or {}
        text = (raw.get("original_text") or m.summary or "").strip().replace("\n", " ")
        try:
            short = pidx.ensure_short_id(m.event_id)
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
@click.option("--no-fill-triggers", is_flag=True, help="不调后端 list_entity_patterns 补 triggers")
def export(fmt: str, output: str | None, include_tombstoned: bool, no_fill_triggers: bool) -> None:
    """导出本地全部记忆到 JSON 或 Markdown。"""
    from pathlib import Path

    from .exporter import export as do_export
    out = do_export(
        fmt=fmt,
        output=Path(output) if output else None,
        include_tombstoned=include_tombstoned,
        fill_triggers=not no_fill_triggers,
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
