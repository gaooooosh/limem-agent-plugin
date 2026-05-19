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
from .eventbus import EventTail, rotate_if_needed
from .learner import (
    archive_old,
    load_suggestions,
    merge_suggestions,
    run_correction_analyzer,
    run_ngram_analyzer,
    save_suggestions,
)
from .lock import FileLock, write_pid
from .state import DaemonState
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
        self.event_tail = EventTail()
        self._correction_buf: list[dict[str, Any]] = []
        self._post_tool_buf: list[dict[str, Any]] = []
        self._shutdown = asyncio.Event()
        self._buf_max = 2000  # 环形 buffer 上限

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
        payload = row.get("payload") or {}
        evidence_seed = json.dumps(row, ensure_ascii=False, sort_keys=True)
        evidence_id = hashlib.sha1(evidence_seed.encode("utf-8")).hexdigest()[:12]
        if kind == "user_prompt_submit":
            prompt = payload.get("prompt", "")
            # F2 correction 采集
            from .learner import is_correction
            if is_correction(prompt) and len(self._correction_buf) < self._buf_max:
                self._correction_buf.append(
                    {
                        "ts": row.get("ts", 0),
                        "project_id": row.get("project_id", ""),
                        "scope": row.get("scope", ""),
                        "prompt": prompt,
                        "session_id": row.get("session_id", ""),
                        "tool": row.get("tool", ""),
                        "evidence_id": evidence_id,
                    }
                )
        elif kind == "post_tool_use":
            if len(self._post_tool_buf) < self._buf_max:
                self._post_tool_buf.append(
                    {
                        "ts": row.get("ts", 0),
                        "project_id": row.get("project_id", ""),
                        "diff_summary": payload.get("diff_summary", ""),
                        "accepted": payload.get("accepted", False),
                        "tool": payload.get("tool", ""),
                        "file_path": payload.get("file_path", ""),
                        "session_id": row.get("session_id", ""),
                        "evidence_id": evidence_id,
                    }
                )

    async def learner_loop(self) -> None:
        period = self.runtime.learner_period_seconds
        while not self._shutdown.is_set():
            try:
                await self._run_learner_once()
            except Exception as e:  # noqa: BLE001
                _log("learner_error", err=str(e))
            await asyncio.sleep(period)

    async def _run_learner_once(self) -> None:
        # F2
        new = run_correction_analyzer(
            self._correction_buf,
            window_seconds=self.runtime.learner_correction_window_hours * 3600,
            jaccard_threshold=self.runtime.learner_jaccard_threshold,
        )
        # F3
        new += run_ngram_analyzer(
            self._post_tool_buf,
            window_seconds=self.runtime.ngram_window_days * 86400,
            min_occurrences=self.runtime.ngram_min_occurrences,
            min_accept_rate=self.runtime.ngram_min_accept_rate,
        )
        if not new:
            return
        existing = load_suggestions()
        merged = merge_suggestions(existing, new)
        merged = archive_old(merged, max_active=self.runtime.suggestions_max_active)
        save_suggestions(merged)
        self.state.suggestion_count = len([s for s in merged if s.get("status") == "pending"])

    async def statusline_loop(self) -> None:
        period = self.runtime.statusline_cache_refresh_seconds
        while not self._shutdown.is_set():
            try:
                self._write_statusline_cache()
            except Exception as e:  # noqa: BLE001
                _log("statusline_loop_error", err=str(e))
            await asyncio.sleep(period)

    def _write_statusline_cache(self) -> None:
        from ..statusline import format_text
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
                    # 简单 GC：截尾 buffer
                    self._correction_buf = self._correction_buf[-500:]
                    self._post_tool_buf = self._post_tool_buf[-500:]
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
