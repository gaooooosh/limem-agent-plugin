"""``limem dash`` 主循环。

模式：
- 默认：多视图 TUI（recalls / active / activity / suggestions），1/2/3/4 或 Tab 切换
- ``--logs``：tail events.ndjson（= activity 视图的独立全屏版本）
- ``--reset-suggestions``：清空 suggestions.json
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .. import daemon_client
from ..config import EVENTS_LOG_PATH, RECENT_RECALLS_PATH, SUGGESTIONS_PATH
from .keys import raw_mode, read_key

console = Console()

# 视图顺序与标签
_VIEWS = ["recalls", "active", "activity", "suggestions"]
_VIEW_LABELS = {
    "recalls": "1·实时召回",
    "active": "2·活跃记忆",
    "activity": "3·活动流",
    "suggestions": "4·待审批",
}


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


def _render_suggestion_detail(suggestions: list[dict[str, Any]], selected: int) -> Panel:
    if not suggestions:
        return Panel(
            "暂无待审批建议。\n按 [r] 立即运行学习器，或用 /limem.remember 手动添加规则。",
            title="detail",
            style="dim",
        )
    selected = max(0, min(selected, len(suggestions) - 1))
    s = suggestions[selected]
    evidence = s.get("evidence") or []
    evidence_lines = "\n".join(f"- {line}" for line in evidence[:6]) or "(no evidence)"
    if len(evidence) > 6:
        evidence_lines += f"\n- ... and {len(evidence) - 6} more"
    body = (
        f"id: {s.get('id', '')}\n"
        f"kind: {s.get('kind', '')}    scope: {s.get('scope', '')}    "
        f"confidence: {s.get('confidence', 0):.2f}\n\n"
        f"candidate:\n{s.get('candidate_text', '')}\n\n"
        f"rationale:\n{s.get('rationale', '(none)')}\n\n"
        f"evidence:\n{evidence_lines}"
    )
    return Panel(body, title=f"detail #{selected}", style="green")


def _render_tabs(view: str) -> Panel:
    parts = []
    for v in _VIEWS:
        label = _VIEW_LABELS[v]
        if v == view:
            parts.append(f"[reverse bold] {label} [/reverse bold]")
        else:
            parts.append(f" {label} ")
    return Panel("  ".join(parts), style="blue")


def _fmt_age(ts: int) -> str:
    if not ts:
        return ""
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    return f"{delta // 86400}天前"


# ---------- 数据获取（容错；失败/不可达返回 None 或空，由渲染层出占位） ----------


def _fetch_recent_recalls(limit: int = 20) -> list[dict[str, Any]] | None:
    """daemon 优先；不可达回退读 recent_recalls.json。返回 None 表示两者皆不可用。"""
    try:
        r = daemon_client.list_recent_recalls(limit)
    except Exception:
        r = None
    if r is not None:
        return r
    try:
        data = json.loads(RECENT_RECALLS_PATH.read_text())
        records = data.get("records") if isinstance(data, dict) else data
        return list(records or [])[:limit]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _fetch_active_memories() -> tuple[list[Any], list[Any]] | None:
    """本地 EntityIndex：返回 (hard_recall metas, principals)。失败返回 None。"""
    try:
        from ..entity_index import EntityIndex
        from ..scope import detect_project_id

        pid = detect_project_id()
        scopes = ["global"] + ([f"project:{pid}"] if pid else [])
        idx = EntityIndex()
        metas = idx.list_hard_recall(
            allowed_scopes=scopes,
            allowed_types=["rule", "feedback", "preference"],
        )
        principals = [p for p in idx.list_principals(active_only=True) if p.has_pattern]
        return metas, principals
    except Exception:
        return None


def _read_events_tail(n: int = 200) -> list[str] | None:
    """读 events 日志尾部 n 行，渲染成人读字符串。文件不存在返回 None。"""
    p = Path(EVENTS_LOG_PATH)
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    out: list[str] = []
    for line in tail.splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = time.strftime("%H:%M:%S", time.localtime(row.get("ts", 0)))
        kind = row.get("kind", "?")
        payload = row.get("payload", {})
        head = ""
        if isinstance(payload, dict):
            head = str(payload.get("prompt") or payload.get("prompt_head") or "")[:50]
        out.append(f"{ts}  {kind}  {head}")
    return out


# ---------- 渲染 ----------


def _render_recalls_table(recalls: list[dict[str, Any]] | None) -> Panel:
    if recalls is None:
        return Panel("📴 daemon off — 无召回缓存", title="实时召回", style="yellow")
    if not recalls:
        return Panel("暂无召回记录（提交一次带规则的对话后会出现）", title="实时召回", style="dim")
    t = Table(expand=True)
    t.add_column("时间", style="dim", no_wrap=True)
    t.add_column("project")
    t.add_column("条数", justify="right")
    t.add_column("来源")
    t.add_column("摘要")
    for rec in recalls[:20]:
        items = rec.get("items") or []
        srcs = {}
        for it in items:
            s = str(it.get("src") or "")
            srcs[s] = srcs.get(s, 0) + 1
        src_str = "/".join(f"{k}{v}" for k, v in srcs.items() if k)
        head = ""
        if items:
            head = " ".join(str(items[0].get("summary_head") or "").split())[:40]
        t.add_row(
            _fmt_age(rec.get("ts", 0)),
            str(rec.get("project_id") or "global")[:24],
            str(len(items)),
            src_str,
            head,
        )
    return Panel(t, title="实时召回（最近注入）", style="cyan")


def _render_active_table(data: tuple[list[Any], list[Any]] | None) -> Panel:
    if data is None:
        return Panel("无法读取本地记忆索引", title="活跃记忆", style="yellow")
    metas, principals = data
    if not metas and not principals:
        return Panel(
            "暂无活跃规则；用 /limem.remember 添加规则，或 /limem.pattern 编辑档案",
            title="活跃记忆",
            style="dim",
        )
    t = Table(expand=True)
    t.add_column("类型", no_wrap=True)
    t.add_column("scope")
    t.add_column("内容")
    t.add_column("imp", justify="right")
    for m in metas[:30]:
        raw = m.raw_metadata or {}
        text = (raw.get("original_text") or m.summary or "").strip().replace("\n", " ")
        t.add_row(m.mem_type, str(m.scope)[:20], text[:60], f"{m.importance:.1f}")
    for p in principals[:10]:
        t.add_row(f"档案·{p.principal_type}", str(p.scope)[:20], (p.canonical or p.slug)[:60], "—")
    title = f"活跃记忆（规则 {len(metas)} · 档案 {len(principals)}）"
    return Panel(t, title=title, style="cyan")


def _render_activity_panel(lines: list[str] | None) -> Panel:
    if lines is None:
        return Panel("暂无活动事件（events 日志尚未生成）", title="活动流", style="dim")
    if not lines:
        return Panel("暂无活动事件", title="活动流", style="dim")
    body = "\n".join(lines[-40:])
    return Panel(body, title="活动流（events tail）", style="cyan")


def _edit_text(initial: str) -> str | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="limem-suggestion-", delete=False) as f:
        path = Path(f.name)
        f.write(initial)
        if initial and not initial.endswith("\n"):
            f.write("\n")
    try:
        subprocess.run([*shlex.split(editor), str(path)], check=False)
        edited = path.read_text(encoding="utf-8").strip()
        return edited or None
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _help_for(view: str) -> str:
    common = "[1-4/Tab] 切视图 · [r] 刷新/学习 · [q] 退出"
    if view == "suggestions":
        return "[j/k] 选择 · [a] 采纳 · [e] 编辑采纳 · [d] 丢弃 · " + common
    return common


def run_dashboard(start_view: str = "recalls") -> int:
    # daemon 不可达也进 TUI：active / activity 走本地数据源仍可用。
    try:
        daemon_client.ensure_or_spawn()
    except Exception:
        pass

    layout = Layout()
    layout.split_column(
        Layout(name="tabs", size=3),
        Layout(name="status", size=8),
        Layout(name="body", ratio=1),
        Layout(name="help", size=3),
    )

    view = start_view if start_view in _VIEWS else "recalls"
    selected = 0

    def _safe_suggestions() -> list[dict[str, Any]]:
        try:
            return daemon_client.list_suggestions() or []
        except Exception:
            return []

    with Live(layout, console=console, refresh_per_second=2, screen=True) as live:
        while True:
            layout["tabs"].update(_render_tabs(view))
            layout["status"].update(_render_status_panel())
            layout["help"].update(Panel(_help_for(view), style="dim"))

            suggestions: list[dict[str, Any]] = []
            if view == "recalls":
                layout["body"].update(_render_recalls_table(_fetch_recent_recalls(20)))
            elif view == "active":
                layout["body"].update(_render_active_table(_fetch_active_memories()))
            elif view == "activity":
                layout["body"].update(_render_activity_panel(_read_events_tail(200)))
            else:  # suggestions
                suggestions = _safe_suggestions()
                selected = max(0, min(selected, len(suggestions) - 1)) if suggestions else 0
                sub = Layout()
                sub.split_column(
                    Layout(_render_suggestions_table(suggestions), name="sg", ratio=2),
                    Layout(_render_suggestion_detail(suggestions, selected), name="dt", ratio=3),
                )
                layout["body"].update(sub)

            with raw_mode():
                key = read_key(timeout=0.5)
            if key is None:
                continue
            if key in ("q", "Q", "\x03"):
                return 0
            if key == "\t":
                view = _VIEWS[(_VIEWS.index(view) + 1) % len(_VIEWS)]
                selected = 0
                continue
            if key in ("1", "2", "3", "4"):
                view = _VIEWS[int(key) - 1]
                selected = 0
                continue
            if key in ("r", "R"):
                # suggestions 视图：触发学习器；其他视图：仅刷新（下一轮自动重读）
                if view == "suggestions":
                    try:
                        daemon_client.run_learner(force=True)
                    except Exception:
                        pass
                continue
            # 以下仅 suggestions 视图生效
            if view != "suggestions":
                continue
            if key in ("j", "J") and suggestions:
                selected = min(len(suggestions) - 1, selected + 1)
                continue
            if key in ("k", "K") and suggestions:
                selected = max(0, selected - 1)
                continue
            if key in ("a", "A") and 0 <= selected < len(suggestions):
                sid = suggestions[selected].get("id")
                if sid:
                    daemon_client.accept_suggestion(sid)
                    continue
            if key in ("e", "E") and 0 <= selected < len(suggestions):
                sid = suggestions[selected].get("id")
                text = suggestions[selected].get("candidate_text", "")
                live.stop()
                try:
                    edited = _edit_text(text)
                finally:
                    live.start(refresh=True)
                if sid and edited and edited != text:
                    daemon_client.accept_suggestion(sid, edited_text=edited)
                elif sid and edited:
                    daemon_client.accept_suggestion(sid)
                continue
            if key in ("d", "D") and 0 <= selected < len(suggestions):
                sid = suggestions[selected].get("id")
                if sid:
                    daemon_client.discard_suggestion(sid)
                    continue
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
