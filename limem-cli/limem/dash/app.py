"""``limem dash`` 主循环。

模式：
- 默认：状态 + 候选列表（按 a/d/e/q 操作）
- ``--logs``：tail events.ndjson
- ``--reset-suggestions``：清空 suggestions.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .. import daemon_client
from ..config import EVENTS_LOG_PATH, SUGGESTIONS_PATH
from .keys import raw_mode, read_key


console = Console()


def _render_status_panel() -> Panel:
    status = daemon_client.get_status() or {}
    if not status:
        return Panel("📴 daemon off — `limem daemon start` to begin", title="status", style="yellow")
    pause = status.get("pause") or {}
    conn = status.get("connectivity") or {}
    lines = [
        f"active memories : {status.get('active_memories', 0)}",
        f"session hits    : {status.get('hit_count', 0)}",
        f"suggestions     : {status.get('suggestion_count', 0)}",
        f"pause           : {'on' if pause.get('on') else 'off'}"
        + (f"  until={pause.get('until_ts')}" if pause.get('on') else ""),
        f"connectivity    : {conn.get('state', 'unknown')}"
        + (f"  ({conn.get('reason')})" if conn.get("reason") else ""),
    ]
    return Panel("\n".join(lines), title="LiMem status", style="cyan")


def _render_suggestions_table(suggestions: list[dict[str, Any]]) -> Table:
    t = Table(title="suggestions (pending)", expand=True)
    t.add_column("#", style="dim")
    t.add_column("id", style="magenta")
    t.add_column("kind")
    t.add_column("scope")
    t.add_column("text")
    t.add_column("conf", justify="right")
    for i, s in enumerate(suggestions[:20]):
        t.add_row(
            str(i),
            s.get("id", "")[:12],
            s.get("kind", ""),
            s.get("scope", ""),
            (s.get("candidate_text", "") or "")[:60],
            f"{s.get('confidence', 0):.2f}",
        )
    return t


def run_dashboard() -> int:
    if not daemon_client.ensure_or_spawn():
        console.print("[red]daemon unreachable; run `limem daemon start`[/red]")
        return 1
    layout = Layout()
    layout.split_column(
        Layout(name="status", size=8),
        Layout(name="suggestions"),
        Layout(name="help", size=3),
    )
    layout["help"].update(
        Panel("[a]ccept N · [d]iscard N · [q]uit · 输入数字选定行", style="dim")
    )

    selected = 0
    suggestions: list[dict[str, Any]] = daemon_client.list_suggestions() or []

    with Live(layout, console=console, refresh_per_second=2, screen=True):
        with raw_mode():
            while True:
                # 刷新数据
                suggestions = daemon_client.list_suggestions() or []
                layout["status"].update(_render_status_panel())
                layout["suggestions"].update(_render_suggestions_table(suggestions))

                key = read_key(timeout=0.5)
                if key is None:
                    continue
                if key in ("q", "Q", "\x03"):
                    return 0
                if key.isdigit():
                    selected = int(key)
                    continue
                if key in ("a", "A") and 0 <= selected < len(suggestions):
                    sid = suggestions[selected].get("id")
                    if sid:
                        daemon_client.accept_suggestion(sid)
                if key in ("d", "D") and 0 <= selected < len(suggestions):
                    sid = suggestions[selected].get("id")
                    if sid:
                        daemon_client.discard_suggestion(sid)
    return 0


def run_logs(*, tail: bool = True) -> int:
    p = Path(EVENTS_LOG_PATH)
    if not p.exists():
        console.print(f"[yellow]no events log at {p}[/yellow]")
        return 0
    pos = 0 if not tail else p.stat().st_size
    while True:
        try:
            time.sleep(0.5)
            cur = p.stat().st_size
            if cur < pos:
                pos = 0  # 滚动
            if cur > pos:
                with p.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read(cur - pos)
                    pos = cur
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        ts = time.strftime("%H:%M:%S", time.localtime(row.get("ts", 0)))
                        console.print(f"[dim]{ts}[/dim] [magenta]{row.get('kind', '?')}[/magenta] {row.get('payload', {})}")
                    except json.JSONDecodeError:
                        console.print(line)
        except KeyboardInterrupt:
            return 0


def reset_suggestions() -> int:
    if SUGGESTIONS_PATH.exists():
        SUGGESTIONS_PATH.unlink()
        console.print("[green]suggestions cleared[/green]")
    else:
        console.print("[dim]no suggestions file[/dim]")
    return 0
