"""跨平台系统通知（best-effort）+ Codex ``notify`` 包装程序。

``notify()`` 是规范的跨平台通知器（notify-send / terminal-notifier / osascript /
powershell），供 daemon auto_init 等复用。

``notify_codex_main()`` 是 Codex ``notify`` 配置指向的程序：Codex 的 notify 是单个
全局外部程序，仅在 ``agent-turn-complete`` 触发，以单个 JSON 字符串作为 argv 传入。
本入口从 daemon 读取本轮召回摘要并推送原生通知（对话外展示）：
- **永不抛、永不非零退出**：任何失败都静默降级（hook systemMessage 仍是基础通道）。
- **链式转发**用户原有 notify 程序（安装时存入 sidecar），避免覆盖用户配置。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .config import CODEX_PREV_NOTIFY_PATH

_SRC_LABEL = {
    "hard": "规则",
    "bm25": "语义",
    "soft": "语义",
    "pattern": "档案",
    "task": "任务",
}


def notify(title: str, message: str) -> bool:
    """返回是否成功发出通知（任何一个 channel 成功即 True）。"""
    # Linux
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-a", "LiMem", title, message],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # macOS：优先 terminal-notifier（多行更友好），回退 osascript
    if shutil.which("terminal-notifier"):
        try:
            subprocess.run(
                ["terminal-notifier", "-title", title, "-message", message, "-group", "limem"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if shutil.which("osascript"):
        try:
            flat = message.replace("\n", " · ")
            script = f"display notification {json.dumps(flat)} with title {json.dumps(title)}"
            subprocess.run(
                ["osascript", "-e", script],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # Windows：powershell toast（BurntToast 不一定可用，做 best-effort）
    if shutil.which("powershell"):
        try:
            flat = message.replace("\n", " ")
            ps = (
                "try { Import-Module BurntToast -ErrorAction Stop; "
                f"New-BurntToastNotification -Text {json.dumps(title)}, {json.dumps(flat)} }} "
                "catch { }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return False


# 别名：语义更清晰的入口（与 notify 同实现）
def os_notify(title: str, body: str) -> bool:
    return notify(title, body)


# ---------- Codex notify 包装 ----------


def _item_label(item: dict[str, Any], *, max_chars: int = 28) -> str:
    """从召回记录的单条 item dict 拼一个紧凑人读标签（不依赖 daemon 类型）。"""
    src = str(item.get("src") or "")
    label = _SRC_LABEL.get(src, src or "记忆")
    summary = " ".join(str(item.get("summary_head") or "").split())
    if not summary and src == "pattern":
        summary = " · ".join(
            part
            for part in (
                str(item.get("canonical") or "").strip(),
                str(item.get("heading") or "").strip(),
            )
            if part
        )
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1] + "…"
    short = str(item.get("short_id") or "")
    ident = f" #{short[:8]}" if short else ""
    return f"{label}{ident} {summary}".strip()


def summarize_recall_record(record: dict[str, Any] | None) -> tuple[str, str] | None:
    """把召回记录 dict 渲染为 (title, body)。无 items 返回 None。"""
    if not record:
        return None
    items = record.get("items") or []
    if not items:
        return None
    n = len(items)
    title = f"LiMem · 本次引用 {n} 条记忆"
    head = items[:3]
    lines = [_item_label(it) for it in head]
    extra = n - len(head)
    if extra > 0:
        lines.append(f"…另 {extra} 条")
    return title, "\n".join(lines)


def pick_recall_record(
    records: list[dict[str, Any]] | None, *, thread_id: str = "", cwd: str = ""
) -> dict[str, Any] | None:
    """从最近召回列表里挑最贴近本轮的一条。

    优先 session_id == thread_id；否则取最新一条（列表头部为最新）。
    """
    if not records:
        return None
    if thread_id:
        for rec in records:
            if str(rec.get("session_id") or "") == thread_id:
                return rec
    return records[0]


def _forward_prev_notify(payload_arg: str) -> None:
    """fire-and-forget 转发到用户原有 notify 程序（若 sidecar 记录了）。"""
    try:
        prev = json.loads(CODEX_PREV_NOTIFY_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(prev, list) or not prev:
        return
    try:
        subprocess.Popen(
            [*[str(x) for x in prev], payload_arg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def notify_codex_main(argv: list[str]) -> int:
    """``limem notify-codex`` 入口。argv 为 Codex 传入的参数（argv[0] 是 payload JSON）。

    永不非零退出，永不抛。
    """
    payload_arg = argv[0] if argv else ""
    # 1) 先转发用户原 notify（不阻塞）
    if payload_arg:
        _forward_prev_notify(payload_arg)

    # 2) LiMem 自身通知
    try:
        payload = json.loads(payload_arg) if payload_arg else {}
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("type") != "agent-turn-complete":
        return 0

    thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "")
    cwd = str(payload.get("cwd") or "")
    try:
        from . import daemon_client

        records = daemon_client.list_recent_recalls(limit=10)
    except Exception:
        records = None
    record = pick_recall_record(records, thread_id=thread_id, cwd=cwd)
    summary = summarize_recall_record(record)
    if not summary:
        return 0
    title, body = summary
    os_notify(title, body)
    return 0
