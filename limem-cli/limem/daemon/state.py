"""Daemon 进程内状态机。

设计：daemon 进程是状态 SoT；磁盘文件是 cache 与"daemon 死透时的 fallback"。
- ``pause.json`` 必须 daemon + 磁盘双写：hook 在 daemon 不可达时仍能从磁盘读
- ``statusline.cache.json`` 由 daemon 每 5s 刷盘
- ``connectivity`` 仅在内存（无 fallback 必要：daemon 死了就 unknown）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    DEGRADED_SEEN_PATH,
    PAUSE_PATH,
)


@dataclass
class ConnectivityState:
    state: str = "unknown"  # unknown | healthy | degraded
    reason: str | None = None
    consec_fail: int = 0
    last_change_ts: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "last_change_ts": self.last_change_ts,
        }


@dataclass
class SessionState:
    session_id: str
    hit_count: int = 0
    started_ts: int = 0
    degraded_banner_emitted: bool = False


@dataclass
class PauseState:
    on: bool = False
    until_ts: int | None = None
    scope: str = "project"
    session_id: str | None = None

    @classmethod
    def load_from_disk(cls) -> "PauseState":
        try:
            data = json.loads(PAUSE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        until = data.get("until_ts")
        if until is not None and until <= int(time.time()):
            return cls()
        return cls(
            on=bool(data.get("on", until is not None)),
            until_ts=until,
            scope=data.get("scope") or "project",
            session_id=data.get("session_id"),
        )

    def save_to_disk(self) -> None:
        PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not self.on:
            try:
                PAUSE_PATH.unlink()
            except FileNotFoundError:
                pass
            return
        payload = {
            "on": True,
            "until_ts": self.until_ts,
            "scope": self.scope,
            "session_id": self.session_id,
        }
        tmp = PAUSE_PATH.with_suffix(PAUSE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(PAUSE_PATH)

    def is_active(self, now_ts: int | None = None) -> bool:
        if not self.on:
            return False
        now = now_ts or int(time.time())
        return self.until_ts is None or self.until_ts > now

    def to_public(self) -> dict[str, Any]:
        return {
            "on": self.is_active(),
            "until_ts": self.until_ts,
            "scope": self.scope,
            "session_id": self.session_id,
        }


@dataclass
class DaemonState:
    started_ts: int = field(default_factory=lambda: int(time.time()))
    active_memories: int = 0
    connectivity: ConnectivityState = field(default_factory=ConnectivityState)
    pause: PauseState = field(default_factory=PauseState)
    sessions: dict[str, SessionState] = field(default_factory=dict)
    suggestion_count: int = 0
    init_pending_until_ts: int | None = None  # F1 dirty repo 提示截止
    inited_now_ts: int | None = None  # F1 刚 init 提示截止

    # ----- session 维度 -----

    def session(self, session_id: str) -> SessionState:
        s = self.sessions.get(session_id)
        if s is None:
            s = SessionState(session_id=session_id, started_ts=int(time.time()))
            self.sessions[session_id] = s
        return s

    def total_hits(self) -> int:
        return sum(s.hit_count for s in self.sessions.values())

    # ----- degraded banner 去重（按 session）-----

    def mark_degraded_banner_emitted(self, session_id: str) -> None:
        self.session(session_id).degraded_banner_emitted = True
        # 落盘以供 hook 跨进程查询（hook 是短进程，每次新启动都要知道）
        path = DEGRADED_SEEN_PATH
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[session_id] = int(time.time())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(path)

    def is_degraded_banner_emitted(self, session_id: str) -> bool:
        s = self.sessions.get(session_id)
        if s and s.degraded_banner_emitted:
            return True
        try:
            data = json.loads(DEGRADED_SEEN_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        return session_id in data

    def gc_degraded_seen(self, *, max_age_seconds: int = 86400) -> int:
        try:
            data = json.loads(DEGRADED_SEEN_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return 0
        now = int(time.time())
        kept = {k: v for k, v in data.items() if now - int(v) < max_age_seconds}
        if len(kept) == len(data):
            return 0
        tmp = DEGRADED_SEEN_PATH.with_suffix(DEGRADED_SEEN_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False))
        tmp.replace(DEGRADED_SEEN_PATH)
        return len(data) - len(kept)


def read_pause_from_disk() -> PauseState:
    """hook 同步路径专用：不依赖 daemon。"""
    return PauseState.load_from_disk()


def is_degraded_banner_emitted_on_disk(session_id: str) -> bool:
    try:
        data = json.loads(Path(DEGRADED_SEEN_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return session_id in data
