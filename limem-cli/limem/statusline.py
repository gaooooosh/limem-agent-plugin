"""``limem statusline`` 子命令实现 + daemon 用 format_text。

总耗时硬约束：< 50ms（P99）。
- 优先 25ms 连接 daemon
- daemon 不通 → 读 statusline.cache.json（daemon 每 5s 刷一次）
- cache 也不通 → 输出 `📴 LiMem daemon off`
"""

from __future__ import annotations

import json
import time

from .config import STATUSLINE_CACHE_PATH


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
    base = " · ".join(parts)
    # F1 提示位
    if init_pending_until_ts and init_pending_until_ts > now:
        return f"{base} · ⚠ init pending"
    if inited_now_ts and inited_now_ts > now:
        return f"{base} · ✓ inited"
    return base


def render() -> str:
    """主入口：返回 statusline 单行文本。"""
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
        )
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return "📴 LiMem daemon off"


def main() -> int:
    print(render())
    return 0
