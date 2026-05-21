"""``limem statusline`` 子命令实现 + daemon 用 format_text。

总耗时硬约束：< 50ms（P99）。
- 优先 25ms 连接 daemon
- daemon 不通 → 读 statusline.cache.json（daemon 每 5s 刷一次）
- cache 也不通 → 输出 `📴 LiMem daemon off`
"""

from __future__ import annotations

import json
import time
from typing import Any

from .config import STATUSLINE_CACHE_PATH, RuntimeConfig


def _format_last_recall(
    last_recall: dict[str, Any] | None,
    *,
    short_ids_max: int = 2,
) -> str:
    """把 daemon 的 last_recall 摘要拼成 statusline 尾段。

    返回空串表示无需追加（last_recall 为空 / count==0）。
    """
    if not last_recall:
        return ""
    count = int(last_recall.get("count") or 0)
    age = _format_age(int(last_recall.get("ts") or 0))
    src = _format_src_counts(last_recall.get("counts_by_src") or {})
    if count <= 0:
        prefix = age or "刚刚"
        return f"✨ {prefix} · 未召回记忆"
    items = list(last_recall.get("items_head") or [])[:short_ids_max]
    if items:
        tail = "；".join(str(x) for x in items if str(x).strip())
        extra = count - len(items)
        suffix = f" (+{extra})" if extra > 0 else ""
        prefix = " · ".join(part for part in (age, src) if part)
        if prefix:
            return f"✨ {prefix} · {tail}{suffix}"
        return f"✨ {tail}{suffix}"

    short_ids = list(last_recall.get("short_ids_head") or [])[:short_ids_max]
    if short_ids:
        tail = " ".join(f"#{s}" for s in short_ids)
        extra = count - len(short_ids)
        prefix = " · ".join(part for part in (age, src) if part)
        if extra > 0:
            return f"✨ {prefix + ' · ' if prefix else ''}{tail} (+{extra})"
        return f"✨ {prefix + ' · ' if prefix else ''}{tail}"
    # 仅有计数（pattern-only 场景：pattern 没有 short_id）
    prefix = " · ".join(part for part in (age, src) if part)
    return f"✨ {prefix + ' · ' if prefix else ''}{count} 条"


def _format_age(ts: int) -> str:
    if ts <= 0:
        return ""
    delta = max(0, int(time.time()) - ts)
    if delta < 60:
        return "刚刚"
    mins = delta // 60
    if mins < 60:
        return f"{mins}分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours}小时前"
    days = hours // 24
    return f"{days}天前"


def _format_src_counts(counts: dict[str, Any]) -> str:
    labels = {
        "hard": "规则",
        "bm25": "语义",
        "soft": "语义",
        "pattern": "档案",
        "task": "任务",
    }
    parts: list[str] = []
    for key in ("hard", "pattern", "bm25", "soft", "task"):
        n = int(counts.get(key) or 0)
        if n <= 0:
            continue
        label = labels.get(key, key)
        if key == "soft" and any(p.startswith("语义") for p in parts):
            continue
        parts.append(f"{label}{n}")
    return "/".join(parts)


def format_text(
    *,
    active: int,
    hits: int,
    sug: int,
    pause_on: bool,
    pause_until_ts: int | None,
    connectivity: str,
    reason: str | None,
    init_pending_until_ts: int | None,
    inited_now_ts: int | None,
    last_recall: dict[str, Any] | None = None,
    last_recall_enabled: bool = True,
    last_recall_short_ids_max: int = 2,
) -> str:
    if connectivity == "degraded":
        return f"⚠ LiMem degraded ({reason or 'unknown'}) · run `limem ping`"
    now = int(time.time())
    extra = ""
    if pause_on:
        if pause_until_ts:
            remain = max(0, pause_until_ts - now)
            mins = remain // 60
            extra = f"⏸ {mins}m"
        else:
            extra = "⏸ ∞"
    else:
        extra = "⏸ off"
    parts = [f"📚 {active}", f"▶ {hits}", f"💡 {sug}", extra]
    if last_recall_enabled:
        piece = _format_last_recall(
            last_recall, short_ids_max=last_recall_short_ids_max
        )
        if piece:
            parts.append(piece)
    base = " · ".join(parts)
    # F1 提示位
    if init_pending_until_ts and init_pending_until_ts > now:
        return f"{base} · ⚠ init pending"
    if inited_now_ts and inited_now_ts > now:
        return f"{base} · ✓ inited"
    return base


def render() -> str:
    """主入口：返回 statusline 单行文本。"""
    # 读 runtime 配置控制 last_recall 是否显示 + 多少个 short_id
    try:
        rt = RuntimeConfig.load()
        last_recall_enabled = bool(rt.statusline_last_recall_enabled)
        last_recall_short_ids_max = int(rt.statusline_last_recall_short_ids_max)
    except Exception:
        last_recall_enabled = True
        last_recall_short_ids_max = 2

    # 尝试 daemon
    try:
        from . import daemon_client
        status = daemon_client.safe_call("get_status")
    except Exception:
        status = None

    if status:
        pause = status.get("pause") or {}
        conn = status.get("connectivity") or {}
        return format_text(
            active=int(status.get("active_memories", 0)),
            hits=int(status.get("hit_count", 0)),
            sug=int(status.get("suggestion_count", 0)),
            pause_on=bool(pause.get("on", False)),
            pause_until_ts=pause.get("until_ts"),
            connectivity=conn.get("state", "unknown"),
            reason=conn.get("reason"),
            init_pending_until_ts=status.get("init_pending_until_ts"),
            inited_now_ts=status.get("inited_now_ts"),
            last_recall=status.get("last_recall"),
            last_recall_enabled=last_recall_enabled,
            last_recall_short_ids_max=last_recall_short_ids_max,
        )

    # fallback：cache.json
    try:
        cache = json.loads(STATUSLINE_CACHE_PATH.read_text())
        raw = cache.get("raw") or {}
        return format_text(
            active=int(raw.get("active", 0)),
            hits=int(raw.get("hits", 0)),
            sug=int(raw.get("sug", 0)),
            pause_on=bool(raw.get("pause", False)),
            pause_until_ts=raw.get("pause_until_ts"),
            connectivity="degraded" if raw.get("degraded") else "unknown",
            reason=raw.get("reason"),
            init_pending_until_ts=raw.get("init_pending_until_ts"),
            inited_now_ts=raw.get("inited_now_ts"),
            last_recall=raw.get("last_recall"),
            last_recall_enabled=last_recall_enabled,
            last_recall_short_ids_max=last_recall_short_ids_max,
        )
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return "📴 LiMem daemon off"


def main() -> int:
    print(render())
    return 0
