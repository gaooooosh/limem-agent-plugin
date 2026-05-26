"""limemd 主进程：unix-socket JSON-RPC + 事件总线消费 + 周期任务。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..client import LimemError
from ..config import (
    LIMEMD_FINGERPRINT_PATH,
    LIMEMD_LOG_PATH,
    LIMEMD_PID_PATH,
    LIMEMD_SOCK_PATH,
    STATUSLINE_CACHE_PATH,
    Credentials,
    RuntimeConfig,
)
from ..pattern_index import PatternIndex
from . import rpc as J
from .auto_init import auto_init as do_auto_init
from .connectivity import (
    classify_status,
    record_failure,
    record_success,
)
from .eventbus import EventTail, emit_event, rotate_if_needed
from .learner import (
    archive_old,
    load_suggestions,
    merge_suggestions,
    run_correction_analyzer,
    run_ngram_analyzer,
    save_suggestions,
)
from .lock import FileLock, write_pid
from .state import DaemonState, RecalledItem, RecallEmittedRecord
from .writer import fix_impl, forget_impl, remember_impl

VERSION = "0.1.0"


def _log(msg: str, **fields: Any) -> None:
    try:
        LIMEMD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": int(time.time()), "msg": msg, **fields}
        with LIMEMD_LOG_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


class Daemon:
    def __init__(self) -> None:
        self.state = DaemonState()
        self.runtime = RuntimeConfig.load()
        self.creds = Credentials.load()
        self.pidx = PatternIndex()
        self._apply_sqlite_pragmas()
        self.state.pause = self.state.pause.load_from_disk()
        self.state.active_memories = self.pidx.stats().get("events_active", 0)
        self.state.suggestion_count = len([s for s in load_suggestions() if s.get("status") == "pending"])
        # 注入 runtime 控制的 recent_recalls 上限，并从磁盘恢复上次环形缓冲
        self.state.set_recent_recalls_max(int(self.runtime.recent_recalls_max))
        self.state.load_recent_recalls_from_disk()
        self.event_tail = EventTail()
        # 环形 buffer：满了自动丢最旧而非丢最新（修复 server.py 旧版本 `len < _buf_max` 丢新事件的 bug）；
        # 实际时间窗剪裁由 learner.run_correction_analyzer / run_ngram_analyzer 在每次 tick 用 window_seconds 完成。
        self._buf_max = 5000
        self._correction_buf: deque[dict[str, Any]] = deque(maxlen=self._buf_max)
        self._post_tool_buf: deque[dict[str, Any]] = deque(maxlen=self._buf_max)
        self._assistant_evidence_by_session: dict[str, dict[str, Any]] = {}
        # PreToolUse → PostToolUse 配对的临时 holding 池；键 = (session_id, file_path)，A1.2 真消费用。
        # 仅 daemon 内存，不写 events.ndjson（守 feedback #b94b0fa：不扩 event schema）。
        self._pending_intents: dict[tuple[str, str], dict[str, Any]] = {}
        # passive learner 只在 learnable event 流 idle 后处理冻结批次，避免周期性重复扫同一窗口。
        self._passive_dirty = False
        self._last_learnable_event_ts = 0
        self._last_processed_correction_ts = 0
        self._last_processed_post_tool_ts = 0
        self._active_passive_batch_hashes: set[str] = set()
        self._learner_wakeup = asyncio.Event()
        self._shutdown = asyncio.Event()

    def _apply_sqlite_pragmas(self) -> None:
        # 限制 SQLite cache（控内存）+ 启用 WAL 让 hook fallback 写也能并发
        try:
            with self.pidx._conn() as conn:  # 借用内部接口
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=200")
                conn.execute("PRAGMA cache_size=-2048")
        except Exception as e:
            _log("sqlite_pragma_failed", err=str(e))

    # ---------- RPC ----------

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                req = J.parse_line(line)
                if not req:
                    writer.write(J.make_error(None, J.INVALID_REQUEST, "invalid json"))
                    await writer.drain()
                    continue
                rid = req.get("id")
                method = req.get("method")
                params = req.get("params") or {}
                if method == "_bye":
                    writer.write(J.make_result(rid, {"ok": True}))
                    await writer.drain()
                    break
                try:
                    result = await self.dispatch(method, params)
                    writer.write(J.make_result(rid, result))
                except _RPCError as e:
                    writer.write(J.make_error(rid, e.code, e.message, e.data))
                except LimemError as e:
                    writer.write(J.make_error(rid, J.INTERNAL_ERROR, str(e), {"status": e.status}))
                except Exception as e:  # noqa: BLE001
                    _log("rpc_error", method=method, err=str(e))
                    writer.write(J.make_error(rid, J.INTERNAL_ERROR, str(e)))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handlers = {
            "_ping": self._h_ping,
            "get_status": self._h_get_status,
            "list_suggestions": self._h_list_suggestions,
            "accept_suggestion": self._h_accept_suggestion,
            "discard_suggestion": self._h_discard_suggestion,
            "bump_hit": self._h_bump_hit,
            "get_pause": self._h_get_pause,
            "set_pause": self._h_set_pause,
            "clear_pause": self._h_clear_pause,
            "set_connectivity": self._h_set_connectivity,
            "get_connectivity": self._h_get_connectivity,
            "write_memory": self._h_write_memory,
            "forget_memory": self._h_forget_memory,
            "fix_memory": self._h_fix_memory,
            "lookup_short_id": self._h_lookup_short_id,
            "auto_init_project": self._h_auto_init_project,
            "report_recall": self._h_report_recall,
            "list_recent_recalls": self._h_list_recent_recalls,
            "seen_recall_keys": self._h_seen_recall_keys,
            "consume_pending_recall": self._h_consume_pending_recall,
            "shutdown": self._h_shutdown,
        }
        h = handlers.get(method)
        if h is None:
            raise _RPCError(J.METHOD_NOT_FOUND, f"unknown method: {method}")
        return await h(params)

    # ----- handlers -----

    async def _h_ping(self, _p: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": VERSION, "pid": os.getpid()}

    async def _h_get_status(self, _p: dict[str, Any]) -> dict[str, Any]:
        return {
            "active_memories": self.state.active_memories,
            "hit_count": self.state.total_hits(),
            "suggestion_count": self.state.suggestion_count,
            "pause": self.state.pause.to_public(),
            "connectivity": self.state.connectivity.to_public(),
            "init_pending_until_ts": self.state.init_pending_until_ts,
            "inited_now_ts": self.state.inited_now_ts,
            "last_recall": self.state.last_recall_to_dict(),
        }

    async def _h_list_suggestions(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        status = p.get("status", "pending")
        items = load_suggestions()
        if status != "all":
            items = [s for s in items if s.get("status") == status]
        return items

    async def _h_accept_suggestion(self, p: dict[str, Any]) -> dict[str, Any]:
        sid = p.get("id")
        edited_text = p.get("edited_text")
        edited_entities = p.get("edited_entities")
        items = load_suggestions()
        for s in items:
            if s.get("id") != sid:
                continue
            if s.get("status") != "pending":
                raise _RPCError(J.INVALID_PARAMS, f"suggestion not pending: {sid}")
            text = edited_text or s.get("candidate_text", "")
            entities = edited_entities or s.get("extracted_entities") or []
            scope = s.get("scope", "global")
            mem_type = s.get("kind", "rule")
            result = remember_impl(
                text=text, scope=scope, mem_type=mem_type,
                importance=0.85,
                entities=entities or None,
                source="daemon:learner_accept",
                creds=self.creds, runtime=self.runtime, idx=self.pidx,
            )
            s["status"] = "accepted"
            s["accepted_event_id"] = result["event_id"]
            save_suggestions(items)
            self.state.suggestion_count = len([x for x in items if x.get("status") == "pending"])
            self.state.active_memories += 1
            return {"event_id": result["event_id"]}
        raise _RPCError(J.INVALID_PARAMS, f"suggestion not found: {sid}")

    async def _h_discard_suggestion(self, p: dict[str, Any]) -> dict[str, Any]:
        sid = p.get("id")
        items = load_suggestions()
        for s in items:
            if s.get("id") == sid:
                s["status"] = "discarded"
                save_suggestions(items)
                self.state.suggestion_count = len([x for x in items if x.get("status") == "pending"])
                return {"ok": True}
        raise _RPCError(J.INVALID_PARAMS, f"suggestion not found: {sid}")

    async def _h_bump_hit(self, p: dict[str, Any]) -> dict[str, Any]:
        sid = p.get("session_id", "")
        self.state.session(sid).hit_count += 1
        return {"ok": True}

    async def _h_get_pause(self, _p: dict[str, Any]) -> dict[str, Any]:
        return self.state.pause.to_public()

    async def _h_set_pause(self, p: dict[str, Any]) -> dict[str, Any]:
        dur = int(p.get("duration_seconds", 3600))
        scope = p.get("scope", "project")
        session_id = p.get("session_id")
        until = int(time.time()) + dur if dur > 0 else None
        self.state.pause.on = True
        self.state.pause.until_ts = until
        self.state.pause.scope = scope
        self.state.pause.session_id = session_id
        self.state.pause.save_to_disk()
        return {"until_ts": until}

    async def _h_clear_pause(self, _p: dict[str, Any]) -> dict[str, Any]:
        self.state.pause.on = False
        self.state.pause.until_ts = None
        self.state.pause.save_to_disk()
        return {"ok": True}

    async def _h_set_connectivity(self, p: dict[str, Any]) -> dict[str, Any]:
        status = int(p.get("status", 0))
        if status == 200 or p.get("ok"):
            switched = record_success(self.state.connectivity)
        else:
            reason = p.get("reason") or classify_status(status)
            switched = record_failure(self.state.connectivity, reason=reason)
        return {"state": self.state.connectivity.state, "switched": switched}

    async def _h_get_connectivity(self, _p: dict[str, Any]) -> dict[str, Any]:
        return self.state.connectivity.to_public()

    async def _h_write_memory(self, p: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: remember_impl(
                text=p["text"],
                scope=p["scope"],
                mem_type=p.get("mem_type", "rule"),
                importance=p.get("importance", 0.9),
                project_id=p.get("project_id", ""),
                entities=p.get("entities"),
                source=p.get("source", "rpc"),
                session_id=p.get("session_id", ""),
                detail=p.get("detail", ""),
                creds=self.creds, runtime=self.runtime, idx=self.pidx,
                skip_redact=p.get("skip_redact", False),
            ),
        )
        self.state.active_memories += 1
        return result

    async def _h_forget_memory(self, p: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: forget_impl(event_id=p["event_id"], creds=self.creds, idx=self.pidx),
        )
        if result.get("local_rows_tombstoned", 0) > 0:
            self.state.active_memories = max(0, self.state.active_memories - 1)
        return result

    async def _h_fix_memory(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fix_impl(
                event_id=p["event_id"],
                new_text=p["new_text"],
                creds=self.creds, idx=self.pidx,
            ),
        )

    async def _h_lookup_short_id(self, p: dict[str, Any]) -> dict[str, Any]:
        short = p.get("short_id", "")
        event_id = self.pidx.lookup_event_by_short_id(short)
        if not event_id:
            raise _RPCError(J.NOT_FOUND_SHORT_ID, f"unknown short_id: {short}")
        return {"event_id": event_id}

    async def _h_auto_init_project(self, p: dict[str, Any]) -> dict[str, Any]:
        cwd = p.get("cwd", os.getcwd())
        result = await asyncio.get_event_loop().run_in_executor(None, lambda: do_auto_init(cwd))
        if result.get("skipped_reason") == "dirty":
            self.state.init_pending_until_ts = int(time.time()) + 300
        elif result.get("created"):
            self.state.inited_now_ts = int(time.time()) + 300
        return result

    async def _h_report_recall(self, p: dict[str, Any]) -> dict[str, Any]:
        """记录一次 hook 的注入产物；同步更新 state + 写一行 recall_emitted 审计。

        envelope schema 不变；recall_emitted 这个 kind 由 ``_handle_event_row`` 入口忽略，
        避免 EventTail 回放重复累加 state。
        """
        items_raw = p.get("items") or []
        items: list[RecalledItem] = []
        for raw in items_raw:
            if not isinstance(raw, dict):
                continue
            items.append(
                RecalledItem(
                    short_id=str(raw.get("short_id") or ""),
                    event_id=str(raw.get("event_id") or ""),
                    src=str(raw.get("src") or ""),
                    mem_type=str(raw.get("mem_type") or ""),
                    scope=str(raw.get("scope") or ""),
                    summary_head=str(raw.get("summary_head") or "")[:60],
                    canonical=str(raw.get("canonical") or ""),
                    heading=str(raw.get("heading") or ""),
                )
            )
        record = RecallEmittedRecord(
            ts=int(p.get("ts") or time.time()),
            session_id=str(p.get("session_id") or ""),
            project_id=str(p.get("project_id") or ""),
            scope=str(p.get("scope") or ""),
            items=items,
            via_patterns=list(p.get("via_patterns") or []),
            via_keywords=list(p.get("via_keywords") or []),
            prompt_head=str(p.get("prompt_head") or "")[:60],
            injected_chars=int(p.get("injected_chars") or 0),
        )
        self.state.record_recall(record)
        # 审计行：写 events.ndjson 但被 _handle_event_row 入口忽略
        counts: dict[str, int] = {}
        for it in record.items:
            if it.src:
                counts[it.src] = counts.get(it.src, 0) + 1
        try:
            emit_event(
                "recall_emitted",
                tool="",
                session_id=record.session_id,
                project_id=record.project_id,
                scope=record.scope,
                payload={
                    "items": [
                        {
                            "short_id": it.short_id,
                            "event_id": it.event_id,
                            "src": it.src,
                            "mem_type": it.mem_type,
                            "scope": it.scope,
                            "summary_head": it.summary_head,
                            "canonical": it.canonical,
                            "heading": it.heading,
                        }
                        for it in record.items
                    ],
                    "via_patterns": record.via_patterns,
                    "via_keywords": record.via_keywords,
                    "prompt_head": record.prompt_head,
                    "injected_chars": record.injected_chars,
                    "counts": counts,
                },
            )
        except Exception as e:  # noqa: BLE001
            _log("recall_emit_audit_failed", err=str(e))
        return {"ok": True}

    async def _h_consume_pending_recall(
        self, p: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Stop hook 用：取出某 session 的待消费 record（取一次后即清除）。

        params: {"session_id": str, "dedupe": bool = True}
        result: 返回 record dict（同 _h_list_recent_recalls 单条 schema），或 null
        """
        session_id = str(p.get("session_id") or "")
        dedupe = bool(p.get("dedupe", True))
        if not session_id:
            return None
        rec = self.state.consume_pending_recall(session_id, dedupe=dedupe)
        if rec is None:
            return None
        return {
            "ts": rec.ts,
            "session_id": rec.session_id,
            "project_id": rec.project_id,
            "scope": rec.scope,
            "prompt_head": rec.prompt_head,
            "injected_chars": rec.injected_chars,
            "via_patterns": list(rec.via_patterns),
            "via_keywords": list(rec.via_keywords),
            "items": [
                {
                    "short_id": it.short_id,
                    "event_id": it.event_id,
                    "src": it.src,
                    "mem_type": it.mem_type,
                    "scope": it.scope,
                    "summary_head": it.summary_head,
                    "canonical": it.canonical,
                    "heading": it.heading,
                }
                for it in rec.items
            ],
        }

    async def _h_seen_recall_keys(self, p: dict[str, Any]) -> dict[str, Any]:
        """UserPromptSubmit 用：返回本 session 已经自动注入过的 memory keys。"""
        session_id = str(p.get("session_id") or "")
        return {"keys": sorted(self.state.seen_recall_keys(session_id))}

    async def _h_list_recent_recalls(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        limit = int(p.get("limit") or 20)
        limit = max(1, min(limit, self.state.recent_recalls_max))
        out: list[dict[str, Any]] = []
        for r in list(self.state.recent_recalls)[:limit]:
            out.append(
                {
                    "ts": r.ts,
                    "session_id": r.session_id,
                    "project_id": r.project_id,
                    "scope": r.scope,
                    "prompt_head": r.prompt_head,
                    "injected_chars": r.injected_chars,
                    "via_patterns": list(r.via_patterns),
                    "via_keywords": list(r.via_keywords),
                    "items": [
                        {
                            "short_id": it.short_id,
                            "event_id": it.event_id,
                            "src": it.src,
                            "mem_type": it.mem_type,
                            "scope": it.scope,
                            "summary_head": it.summary_head,
                            "canonical": it.canonical,
                            "heading": it.heading,
                        }
                        for it in r.items
                    ],
                }
            )
        return out

    async def _h_shutdown(self, _p: dict[str, Any]) -> dict[str, Any]:
        self._shutdown.set()
        return {"ok": True}

    # ---------- 后台任务 ----------

    async def consume_events_loop(self) -> None:
        period = 1.0
        while not self._shutdown.is_set():
            try:
                for row in self.event_tail.poll():
                    self._handle_event_row(row)
            except Exception as e:  # noqa: BLE001
                _log("consume_events_error", err=str(e))
            await asyncio.sleep(period)

    def _handle_event_row(self, row: dict[str, Any]) -> None:
        kind = row.get("kind")
        # daemon 自己 emit 的审计行，不回放（防 state 重复累加）
        if kind == "recall_emitted":
            return
        payload = row.get("payload") or {}
        evidence_seed = json.dumps(row, ensure_ascii=False, sort_keys=True)
        evidence_id = hashlib.sha1(evidence_seed.encode("utf-8")).hexdigest()[:12]
        if kind == "user_prompt_submit":
            prompt = payload.get("prompt", "")
            # F2 correction 采集；prev_assistant_head 由 hooks.py UserPromptSubmit 可选填充（A4）。
            from .learner import score_correction
            prev = payload.get("prev_assistant_head") or None
            if not prev:
                prev = Daemon._consume_assistant_evidence(
                    self,
                    row.get("session_id", ""),
                    row.get("ts", 0),
                )
            ok, conf = score_correction(prompt, prev_assistant=prev)
            if ok and conf >= self.runtime.is_correction_confidence_threshold:
                # deque(maxlen=...) 自动丢最旧；无需 len 守门，避免丢最新事件
                self._correction_buf.append(
                    {
                        "ts": row.get("ts", 0),
                        "project_id": row.get("project_id", ""),
                        "scope": row.get("scope", ""),
                        "prompt": prompt,
                        "prev_assistant_head": prev or "",
                        "is_correction_confidence": conf,
                        "session_id": row.get("session_id", ""),
                        "tool": row.get("tool", ""),
                        "evidence_id": evidence_id,
                    }
                )
                Daemon._mark_passive_dirty(self, row.get("ts", 0))
        elif kind == "assistant_evidence":
            Daemon._remember_assistant_evidence(self, row)
        elif kind == "pre_tool_use":
            # A1.2：仅 Edit/Write/NotebookEdit 进配对池（Bash 等不参与，亦不携带 intent_summary）
            tool_name = (payload.get("tool") or "").strip()
            if tool_name in {"Edit", "Write", "NotebookEdit"}:
                key = (row.get("session_id", ""), payload.get("file_path", "") or "")
                self._pending_intents[key] = {
                    "ts": row.get("ts", 0),
                    "intent_summary": payload.get("intent_summary", ""),
                    "tool": tool_name,
                }
                self._evict_old_intents(now=row.get("ts", 0))
        elif kind == "post_tool_use":
            buf_item: dict[str, Any] = {
                "ts": row.get("ts", 0),
                "project_id": row.get("project_id", ""),
                "diff_summary": payload.get("diff_summary", ""),
                "accepted": payload.get("accepted", False),
                "tool": payload.get("tool", ""),
                "file_path": payload.get("file_path", ""),
                "session_id": row.get("session_id", ""),
                "evidence_id": evidence_id,
            }
            # A1.2：找最近 N 秒同 session_id+file_path 的 pre_tool_use，合并 intent_summary 到 buffer item
            key = (row.get("session_id", ""), payload.get("file_path", "") or "")
            intent = self._pending_intents.pop(key, None)
            if intent:
                pair_age = row.get("ts", 0) - intent["ts"]
                if 0 <= pair_age <= self.runtime.pre_post_pair_window_seconds:
                    buf_item["intent_summary"] = intent["intent_summary"]
                    buf_item["pair_age_seconds"] = pair_age
            self._post_tool_buf.append(buf_item)
            if buf_item.get("accepted") and (buf_item.get("diff_summary") or buf_item.get("intent_summary")):
                Daemon._mark_passive_dirty(self, row.get("ts", 0))
        elif kind == "session_end":
            self._learner_wakeup.set()

    def _mark_passive_dirty(self, ts: int | None = None) -> None:
        try:
            self._passive_dirty = True
            self._last_learnable_event_ts = max(
                int(getattr(self, "_last_learnable_event_ts", 0) or 0),
                int(ts or time.time()),
            )
            self._learner_wakeup.set()
        except Exception:
            pass

    def _remember_assistant_evidence(self, row: dict[str, Any]) -> None:
        session_id = str(row.get("session_id") or "")
        if not session_id:
            return
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        content = " ".join(str(payload.get("content") or "").split())
        if not content:
            return
        store = getattr(self, "_assistant_evidence_by_session", None)
        if store is None:
            return
        store[session_id] = {
            "ts": int(row.get("ts", 0) or 0),
            "content": content,
            "content_hash": str(payload.get("content_hash") or ""),
            "source": str(payload.get("source") or ""),
        }
        if len(store) > 200:
            for sid, _ev in sorted(store.items(), key=lambda kv: kv[1].get("ts", 0))[: len(store) - 200]:
                store.pop(sid, None)

    def _consume_assistant_evidence(self, session_id: str, prompt_ts: int | None) -> str:
        store = getattr(self, "_assistant_evidence_by_session", None)
        if not store or not session_id:
            return ""
        ev = store.pop(session_id, None)
        if not ev:
            return ""
        ev_ts = int(ev.get("ts", 0) or 0)
        cur_ts = int(prompt_ts or 0)
        if cur_ts and ev_ts and cur_ts - ev_ts > 3600:
            return ""
        return str(ev.get("content") or "")

    def _evict_old_intents(self, *, now: int) -> None:
        """清理过期的 pre_tool_use 配对项；上限 200 条防止无界增长。"""
        window = self.runtime.pre_post_pair_window_seconds
        stale = [k for k, v in self._pending_intents.items() if now - v["ts"] > window]
        for k in stale:
            self._pending_intents.pop(k, None)
        # 防御性硬上限：按 ts 升序丢最旧
        if len(self._pending_intents) > 200:
            ordered = sorted(self._pending_intents.items(), key=lambda kv: kv[1]["ts"])
            for k, _v in ordered[: len(self._pending_intents) - 200]:
                self._pending_intents.pop(k, None)

    async def learner_loop(self) -> None:
        period = min(
            max(1, int(self.runtime.learner_period_seconds)),
            max(1, int(self.runtime.passive_learning_idle_seconds)),
        )
        while not self._shutdown.is_set():
            try:
                await self._run_learner_if_idle()
            except Exception as e:  # noqa: BLE001
                _log("learner_error", err=str(e))
            try:
                await asyncio.wait_for(self._learner_wakeup.wait(), timeout=period)
                self._learner_wakeup.clear()
            except asyncio.TimeoutError:
                pass

    async def _run_learner_if_idle(self) -> None:
        if not self.runtime.passive_learning_enabled:
            return
        if not getattr(self, "_passive_dirty", False):
            return
        last_ts = int(getattr(self, "_last_learnable_event_ts", 0) or 0)
        if last_ts and int(time.time()) - last_ts < int(self.runtime.passive_learning_idle_seconds):
            return
        await self._run_learner_once()

    async def _run_learner_once(self) -> None:
        min_events = max(1, int(getattr(self.runtime, "passive_learning_min_events", 1) or 1))
        correction_events = [
            e for e in list(self._correction_buf)
            if int(e.get("ts", 0) or 0) > int(getattr(self, "_last_processed_correction_ts", 0) or 0)
        ]
        post_tool_events = [
            e for e in list(self._post_tool_buf)
            if int(e.get("ts", 0) or 0) > int(getattr(self, "_last_processed_post_tool_ts", 0) or 0)
        ]
        correction_ready = len(correction_events) >= max(2, min_events)
        post_tool_ready = len(post_tool_events) >= max(
            1,
            int(getattr(self.runtime, "ngram_min_occurrences", 1) or 1),
        )
        if not correction_ready and not post_tool_ready:
            return
        analyzer_correction_events = correction_events if correction_ready else []
        analyzer_post_tool_events = post_tool_events if post_tool_ready else []
        batch_hash = self._passive_batch_hash(correction_events, post_tool_events)
        if batch_hash in getattr(self, "_active_passive_batch_hashes", set()):
            self._passive_dirty = False
            return
        # F2
        new = run_correction_analyzer(
            analyzer_correction_events,
            window_seconds=self.runtime.learner_correction_window_hours * 3600,
            jaccard_threshold=self.runtime.learner_jaccard_threshold,
        )
        # F3
        new += run_ngram_analyzer(
            analyzer_post_tool_events,
            window_seconds=self.runtime.ngram_window_days * 86400,
            min_occurrences=self.runtime.ngram_min_occurrences,
            min_accept_rate=self.runtime.ngram_min_accept_rate,
        )
        for s in new:
            meta = dict(s.get("metadata") or {})
            meta["passive_batch_hash"] = batch_hash
            s["metadata"] = meta
        existing = load_suggestions()
        merged = merge_suggestions(existing, new) if new else existing
        learned = 0
        if merged and self.runtime.passive_learning_auto_submit:
            learned = await self._submit_passive_suggestions(merged, batch_hash=batch_hash)
        if merged:
            merged = archive_old(merged, max_active=self.runtime.suggestions_max_active)
            save_suggestions(merged)
            self.state.suggestion_count = len([s for s in merged if s.get("status") == "pending"])
        if analyzer_correction_events:
            self._last_processed_correction_ts = max(int(e.get("ts", 0) or 0) for e in correction_events)
        if analyzer_post_tool_events:
            self._last_processed_post_tool_ts = max(int(e.get("ts", 0) or 0) for e in post_tool_events)
        self._active_passive_batch_hashes.add(batch_hash)
        self._passive_dirty = False
        if learned:
            self.state.active_memories += learned

    def _passive_batch_hash(
        self,
        correction_events: list[dict[str, Any]],
        post_tool_events: list[dict[str, Any]],
    ) -> str:
        body = {
            "correction": [
                {
                    "ts": e.get("ts", 0),
                    "session_id": e.get("session_id", ""),
                    "evidence_id": e.get("evidence_id", ""),
                    "prompt": e.get("prompt", ""),
                    "prev": e.get("prev_assistant_head", ""),
                }
                for e in correction_events
            ],
            "post_tool": [
                {
                    "ts": e.get("ts", 0),
                    "session_id": e.get("session_id", ""),
                    "evidence_id": e.get("evidence_id", ""),
                    "diff": e.get("diff_summary", ""),
                    "intent": e.get("intent_summary", ""),
                }
                for e in post_tool_events
            ],
        }
        digest = hashlib.sha1(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"plb_{digest}"

    async def _submit_passive_suggestions(
        self,
        items: list[dict[str, Any]],
        *,
        batch_hash: str = "",
    ) -> int:
        """Submit pending passive-learning candidates to LiMem service as event memories.

        The daemon keeps suggestions.json as an audit/retry ledger. The source of truth for
        learned memory is the service-side event created by remember_impl -> /ingest.
        """
        if not self.creds.api_key or not self.creds.db_id:
            return 0

        learned = 0
        loop = asyncio.get_event_loop()
        for suggestion in items:
            if suggestion.get("status") != "pending":
                continue
            meta = suggestion.get("metadata") if isinstance(suggestion.get("metadata"), dict) else {}
            if batch_hash and meta.get("passive_batch_hash") != batch_hash:
                continue
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda s=suggestion: remember_impl(
                        text=self._passive_learning_text(s),
                        scope=s.get("scope", "global"),
                        mem_type=s.get("kind", "rule"),
                        importance=float(s.get("confidence", 0.85) or 0.85),
                        project_id=self._project_id_from_scope(s.get("scope", "")),
                        entities=s.get("extracted_entities") or None,
                        source="daemon:passive_learning",
                        detail=self._passive_learning_detail(s),
                        creds=self.creds,
                        runtime=self.runtime,
                        idx=self.pidx,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                suggestion["last_error"] = str(e)[:200]
                suggestion["last_error_ts"] = int(time.time())
                _log(
                    "passive_learning_submit_error",
                    suggestion_id=suggestion.get("id", ""),
                    err=str(e)[:200],
                )
                continue

            event_id = result.get("event_id", "")
            suggestion["status"] = "learned"
            suggestion["learned_event_id"] = event_id
            suggestion["learned_ts"] = int(time.time())
            suggestion.pop("last_error", None)
            suggestion.pop("last_error_ts", None)
            learned += 1
            _log(
                "passive_learning_submitted",
                suggestion_id=suggestion.get("id", ""),
                event_id=event_id,
                kind=suggestion.get("kind", ""),
                scope=suggestion.get("scope", ""),
            )
        return learned

    def _passive_learning_text(self, suggestion: dict[str, Any]) -> str:
        return str(suggestion.get("candidate_text") or "").strip()

    def _passive_learning_detail(self, suggestion: dict[str, Any]) -> str:
        evidence = suggestion.get("evidence") or []
        rationale = str(suggestion.get("rationale") or "").strip()
        parts = [
            "passive learning observation",
            f"rationale: {rationale}" if rationale else "",
        ]
        if evidence:
            parts.append("evidence:")
            parts.extend(f"- {line}" for line in evidence[:8])
        return "\n".join(part for part in parts if part)

    def _project_id_from_scope(self, scope: str) -> str:
        if scope.startswith("project:"):
            return scope.split(":", 1)[1]
        return ""

    async def statusline_loop(self) -> None:
        period = self.runtime.statusline_cache_refresh_seconds
        while not self._shutdown.is_set():
            try:
                self._write_statusline_cache()
                # 顺带刷一次 recent_recalls.json（开销 < 1ms，省一个独立 loop）
                self.state.save_recent_recalls_to_disk()
            except Exception as e:  # noqa: BLE001
                _log("statusline_loop_error", err=str(e))
            await asyncio.sleep(period)

    def _write_statusline_cache(self) -> None:
        from ..statusline import format_text
        last_recall = self.state.last_recall_to_dict()
        text = format_text(
            active=self.state.active_memories,
            hits=self.state.total_hits(),
            sug=self.state.suggestion_count,
            pause_on=self.state.pause.is_active(),
            pause_until_ts=self.state.pause.until_ts,
            connectivity=self.state.connectivity.state,
            reason=self.state.connectivity.reason,
            init_pending_until_ts=self.state.init_pending_until_ts,
            inited_now_ts=self.state.inited_now_ts,
            last_recall=last_recall,
            last_recall_enabled=bool(self.runtime.statusline_last_recall_enabled),
            last_recall_short_ids_max=int(
                self.runtime.statusline_last_recall_short_ids_max
            ),
        )
        payload = {
            "ts": int(time.time()),
            "text": text,
            "raw": {
                "active": self.state.active_memories,
                "hits": self.state.total_hits(),
                "sug": self.state.suggestion_count,
                "pause": self.state.pause.is_active(),
                "pause_until_ts": self.state.pause.until_ts,
                "degraded": self.state.connectivity.state == "degraded",
                "reason": self.state.connectivity.reason,
                "init_pending_until_ts": self.state.init_pending_until_ts,
                "inited_now_ts": self.state.inited_now_ts,
                "last_recall": last_recall,
            },
        }
        STATUSLINE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUSLINE_CACHE_PATH.with_suffix(STATUSLINE_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(STATUSLINE_CACHE_PATH)

    async def housekeeping_loop(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(300)
            try:
                # 内存自检
                rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # macOS ru_maxrss 单位是 bytes，Linux 是 KB；统一按 KB 解释（macOS 多除 1024 即可）
                rss_mb = rss_kb / 1024
                if sys.platform == "darwin":
                    rss_mb = rss_kb / (1024 * 1024)
                if rss_mb > self.runtime.daemon_rss_soft_limit_mb:
                    _log("rss_high", rss_mb=rss_mb)
                    # 紧急 GC：将 deque 缩到尾部 500 条；deque 不支持负数切片，借助构造新 deque 实现
                    self._correction_buf = deque(
                        list(self._correction_buf)[-500:], maxlen=self._buf_max
                    )
                    self._post_tool_buf = deque(
                        list(self._post_tool_buf)[-500:], maxlen=self._buf_max
                    )
                    self._pending_intents.clear()
                # 日志滚动
                if rotate_if_needed(
                    max_bytes=self.runtime.events_log_max_bytes,
                    max_age_seconds=self.runtime.events_log_max_age_days * 86400,
                ):
                    _log("events_log_rotated")
                # degraded_seen GC
                gc_count = self.state.gc_degraded_seen()
                if gc_count:
                    _log("degraded_seen_gc", removed=gc_count)
            except Exception as e:  # noqa: BLE001
                _log("housekeeping_error", err=str(e))


class _RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data


# ---------- 启动 ----------


def _write_fingerprint() -> None:
    LIMEMD_FINGERPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIMEMD_FINGERPRINT_PATH.write_text(
        json.dumps({"pid": os.getpid(), "started_ts": int(time.time()), "version": VERSION})
    )


async def _serve() -> None:
    sock_path = LIMEMD_SOCK_PATH
    # 旧 socket 清理（前任 daemon crash 留下的）
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass

    daemon = Daemon()
    server = await asyncio.start_unix_server(daemon.handle_client, path=str(sock_path))
    os.chmod(sock_path, 0o700)
    write_pid(LIMEMD_PID_PATH)
    _write_fingerprint()
    _log("daemon_started", pid=os.getpid(), sock=str(sock_path))

    bg = [
        asyncio.create_task(daemon.consume_events_loop()),
        asyncio.create_task(daemon.learner_loop()),
        asyncio.create_task(daemon.statusline_loop()),
        asyncio.create_task(daemon.housekeeping_loop()),
    ]

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon._shutdown.set)
        except NotImplementedError:
            pass

    try:
        await daemon._shutdown.wait()
    finally:
        _log("daemon_stopping")
        for t in bg:
            t.cancel()
        server.close()
        await server.wait_closed()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        try:
            LIMEMD_PID_PATH.unlink()
            LIMEMD_FINGERPRINT_PATH.unlink()
        except FileNotFoundError:
            pass


def _detach_and_run() -> None:
    """简单 detach：double-fork + setsid + 重定向 stdio。"""
    if os.fork() != 0:
        return  # 父进程返回
    os.setsid()
    if os.fork() != 0:
        os._exit(0)  # 中间进程退出
    # 孙进程：重定向 stdio
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", "rb") as r:
        os.dup2(r.fileno(), sys.stdin.fileno())
    with open(LIMEMD_LOG_PATH, "ab", buffering=0) as w:
        LIMEMD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.dup2(w.fileno(), sys.stdout.fileno())
        os.dup2(w.fileno(), sys.stderr.fileno())
    _run_forever_with_lock()


def _run_forever_with_lock() -> None:
    lock = FileLock(Path(str(LIMEMD_PID_PATH) + ".lock"))
    if not lock.acquire():
        _log("already_running")
        os._exit(0)
    try:
        asyncio.run(_serve())
    finally:
        lock.release()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="limemd")
    parser.add_argument("--detach", action="store_true", help="后台启动（double-fork）")
    args = parser.parse_args(argv)

    if args.detach:
        _detach_and_run()
        return 0

    lock_path = Path(str(LIMEMD_PID_PATH) + ".lock")
    lock = FileLock(lock_path)
    if not lock.acquire():
        # 已有 daemon 在跑
        from .lock import read_pid as _rp
        pid = _rp(LIMEMD_PID_PATH)
        print(f"already running pid={pid}", file=sys.stderr)
        return 0
    try:
        asyncio.run(_serve())
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(run())
