"""Daemon 进程内状态机。

设计：daemon 进程是状态 SoT；磁盘文件是 cache 与"daemon 死透时的 fallback"。
- ``pause.json`` 必须 daemon + 磁盘双写：hook 在 daemon 不可达时仍能从磁盘读
- ``statusline.cache.json`` 由 daemon 每 5s 刷盘
- ``connectivity`` 仅在内存（无 fallback 必要：daemon 死了就 unknown）
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    DEGRADED_SEEN_PATH,
    PAUSE_PATH,
    RECENT_RECALLS_PATH,
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
    def load_from_disk(cls) -> PauseState:
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
class RecalledItem:
    """单条「本轮注入」记录；hard/soft 走 event_id+short_id，pattern 走 canonical+heading。"""

    short_id: str = ""  # 不带 "#" 前缀；pattern src 可空
    event_id: str = ""  # pattern src 可空
    src: str = ""  # "hard" | "pattern" | "bm25" | "task"
    mem_type: str = ""  # rule / feedback / preference / note / ""
    scope: str = ""
    summary_head: str = ""  # ≤60 chars
    canonical: str = ""  # 仅 pattern
    heading: str = ""  # 仅 pattern


def recall_item_key(item: RecalledItem) -> str:
    """Session-level de-dupe key for memories already injected into the model."""
    if item.event_id:
        return f"event:{item.event_id}"
    if item.short_id:
        return f"short:{item.short_id}"
    if item.src == "pattern" and (item.canonical or item.heading):
        return f"pattern:{item.canonical}:{item.heading}"
    if item.src == "task" and item.summary_head:
        return f"task:{item.summary_head}"
    if item.src and item.summary_head:
        return f"{item.src}:{item.summary_head}"
    return ""


@dataclass
class RecallEmittedRecord:
    """一次 UserPromptSubmit 注入产出的元数据快照，供 dash / statusline 反向消费。"""

    ts: int = 0
    session_id: str = ""
    project_id: str = ""
    scope: str = ""
    items: list[RecalledItem] = field(default_factory=list)
    via_patterns: list[str] = field(default_factory=list)
    via_keywords: list[str] = field(default_factory=list)
    prompt_head: str = ""  # ≤60 chars
    injected_chars: int = 0


@dataclass
class LastRecallSummary:
    """statusline 直接消费的最小摘要；存活在 DaemonState.last_recall。"""

    ts: int = 0
    count: int = 0
    short_ids_head: list[str] = field(default_factory=list)  # 最多 2 个
    counts_by_src: dict[str, int] = field(default_factory=dict)


def _record_from_dict(data: dict[str, Any]) -> RecallEmittedRecord:
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        items.append(
            RecalledItem(
                short_id=str(it.get("short_id") or ""),
                event_id=str(it.get("event_id") or ""),
                src=str(it.get("src") or ""),
                mem_type=str(it.get("mem_type") or ""),
                scope=str(it.get("scope") or ""),
                summary_head=str(it.get("summary_head") or ""),
                canonical=str(it.get("canonical") or ""),
                heading=str(it.get("heading") or ""),
            )
        )
    return RecallEmittedRecord(
        ts=int(data.get("ts") or 0),
        session_id=str(data.get("session_id") or ""),
        project_id=str(data.get("project_id") or ""),
        scope=str(data.get("scope") or ""),
        items=items,
        via_patterns=list(data.get("via_patterns") or []),
        via_keywords=list(data.get("via_keywords") or []),
        prompt_head=str(data.get("prompt_head") or ""),
        injected_chars=int(data.get("injected_chars") or 0),
    )


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
    # 「最近召回」环形缓冲（newest-first appendleft）；maxlen 在 record_recall 内强制
    recent_recalls: deque[RecallEmittedRecord] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    # statusline 摘要（仅最近一轮的精简形态）
    last_recall: LastRecallSummary | None = None
    # 缓冲上限（不可在 dataclass field 内引用 RuntimeConfig；由 Daemon.__init__ 注入）
    recent_recalls_max: int = 20
    # Stop hook 主动提示链路：session_id -> 待消费的 RecallEmittedRecord（每次 report_recall 时刷新）
    # 注意：仅在内存中维护；daemon 重启后丢失（下一次注入会重新填充，无回放需求）
    pending_recall_by_session: dict[str, RecallEmittedRecord] = field(default_factory=dict)
    # Stop hook 去重签名：session_id -> 上次已展示给用户的 record 签名（hash of short_ids+counts）
    last_displayed_signature_by_session: dict[str, str] = field(default_factory=dict)
    # UserPromptSubmit 自动召回去重：session_id -> 已注入过的 memory keys
    seen_recall_keys_by_session: dict[str, set[str]] = field(default_factory=dict)

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

    # ----- recent_recalls -----

    def set_recent_recalls_max(self, max_len: int) -> None:
        """由 Daemon.__init__ 注入 runtime 配置后调用，重建 deque。"""
        max_len = max(1, int(max_len))
        if max_len == self.recent_recalls.maxlen:
            self.recent_recalls_max = max_len
            return
        new_deque: deque[RecallEmittedRecord] = deque(maxlen=max_len)
        # 保留最新 max_len 条
        for rec in list(self.recent_recalls)[:max_len]:
            new_deque.append(rec)
        self.recent_recalls = new_deque
        self.recent_recalls_max = max_len

    def record_recall(self, record: RecallEmittedRecord) -> None:
        """记录一次注入；newest-first 入队 + 更新 last_recall 摘要 + 刷新 session pending。"""
        self.recent_recalls.appendleft(record)
        counts: dict[str, int] = {}
        short_ids: list[str] = []
        for it in record.items:
            if it.src:
                counts[it.src] = counts.get(it.src, 0) + 1
            if it.short_id:
                short_ids.append(it.short_id)
        self.last_recall = LastRecallSummary(
            ts=record.ts,
            count=len(record.items),
            short_ids_head=short_ids[:2],
            counts_by_src=counts,
        )
        # Stop hook 主动提示用：把该 session 的最新 record 标记为 pending（未消费）。
        # 若同一 session 两轮间均无 Stop 触达，新 record 覆盖旧的 —— 这是预期行为：用户只关心最近一轮。
        if record.session_id:
            self.pending_recall_by_session[record.session_id] = record
            seen = self.seen_recall_keys_by_session.setdefault(record.session_id, set())
            for item in record.items:
                key = recall_item_key(item)
                if key:
                    seen.add(key)

    def seen_recall_keys(self, session_id: str) -> set[str]:
        if not session_id:
            return set()
        return set(self.seen_recall_keys_by_session.get(session_id) or set())

    @staticmethod
    def _record_signature(record: RecallEmittedRecord) -> str:
        """同内容判定签名：用 (sorted short_ids tuple + counts_by_src) 做 hash。

        判定语义：两轮注入的 short_id 集合与各源条数完全一致即视为「相同」，
        不引入纯文本对比（避免顺序、scope 等抖动）。
        """
        import hashlib
        short_ids = sorted({it.short_id for it in record.items if it.short_id})
        counts: dict[str, int] = {}
        for it in record.items:
            if it.src:
                counts[it.src] = counts.get(it.src, 0) + 1
        seed = (
            "|".join(short_ids)
            + "::"
            + ";".join(f"{k}={counts[k]}" for k in sorted(counts.keys()))
            + f"::pattern_only={1 if not short_ids and record.items else 0}"
        )
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def consume_pending_recall(
        self, session_id: str, *, dedupe: bool = True
    ) -> RecallEmittedRecord | None:
        """取出该 session 待消费的 record；调用后从 pending 中移除（防止重复推送）。

        - 无 pending → None
        - dedupe=True 且签名与上次已展示完全相同 → None（去重）
        - 否则返回 record 并更新 last_displayed_signature_by_session[session_id]
        """
        if not session_id:
            return None
        rec = self.pending_recall_by_session.pop(session_id, None)
        if rec is None:
            return None
        if dedupe:
            sig = self._record_signature(rec)
            last = self.last_displayed_signature_by_session.get(session_id)
            if sig == last:
                return None
            self.last_displayed_signature_by_session[session_id] = sig
        return rec

    def last_recall_to_dict(self) -> dict[str, Any] | None:
        return asdict(self.last_recall) if self.last_recall is not None else None

    def load_recent_recalls_from_disk(self) -> None:
        """daemon 启动时调用；文件不存在 / 损坏均静默忽略，state 保持空。"""
        try:
            data = json.loads(RECENT_RECALLS_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        records_raw = data.get("records") or []
        if not isinstance(records_raw, list):
            return
        max_len = self.recent_recalls.maxlen or 20
        new_deque: deque[RecallEmittedRecord] = deque(maxlen=max_len)
        for r in records_raw[:max_len]:
            if not isinstance(r, dict):
                continue
            try:
                new_deque.append(_record_from_dict(r))
            except Exception:
                continue
        self.recent_recalls = new_deque
        # 重算 last_recall 摘要（直接读 deque 头一条，不再 appendleft）
        if new_deque:
            latest = new_deque[0]
            counts: dict[str, int] = {}
            short_ids: list[str] = []
            for it in latest.items:
                if it.src:
                    counts[it.src] = counts.get(it.src, 0) + 1
                if it.short_id:
                    short_ids.append(it.short_id)
            self.last_recall = LastRecallSummary(
                ts=latest.ts,
                count=len(latest.items),
                short_ids_head=short_ids[:2],
                counts_by_src=counts,
            )

    def save_recent_recalls_to_disk(self) -> None:
        """原子写 RECENT_RECALLS_PATH；按 PauseState.save_to_disk 范式。"""
        path = RECENT_RECALLS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_ts": int(time.time()),
            "records": [asdict(r) for r in list(self.recent_recalls)],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            tmp.replace(path)
        except OSError:
            # 落盘失败不致命；下次周期再试
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def read_pause_from_disk() -> PauseState:
    """hook 同步路径专用：不依赖 daemon。"""
    return PauseState.load_from_disk()


def is_degraded_banner_emitted_on_disk(session_id: str) -> bool:
    try:
        data = json.loads(Path(DEGRADED_SEEN_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return session_id in data
