"""daemon_client — 同步 unix-socket JSON-RPC 客户端，供 hook/MCP/CLI 使用。

接口设计：
- 短超时（默认 200ms 调用 / 25ms connect）
- daemon 不可达时返回 None；调用方决定 fallback 行为
- 自动拉起：``try_spawn_daemon()`` 静默 fork
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from .config import (
    LIMEMD_FORK_LOCK_PATH,
    LIMEMD_SOCK_PATH,
    STATUSLINE_CACHE_PATH,
)
from .daemon.lock import FileLock


class DaemonUnavailable(Exception):
    pass


def _read_cache() -> dict[str, Any] | None:
    try:
        return json.loads(STATUSLINE_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    connect_timeout_ms: int = 25,
    call_timeout_ms: int = 200,
) -> Any:
    """同步 RPC。失败抛 DaemonUnavailable。"""
    if not LIMEMD_SOCK_PATH.exists():
        raise DaemonUnavailable("socket not present")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(connect_timeout_ms / 1000.0)
        s.connect(str(LIMEMD_SOCK_PATH))
        s.settimeout(call_timeout_ms / 1000.0)
        body = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        s.sendall(body)
        # 读直到换行
        buf = bytearray()
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        line = bytes(buf).split(b"\n", 1)[0]
        if not line:
            raise DaemonUnavailable("empty response")
        resp = json.loads(line)
        if "error" in resp:
            err = resp["error"]
            raise RPCError(int(err.get("code", -1)), err.get("message", ""), err.get("data"))
        return resp.get("result")
    except (TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise DaemonUnavailable(str(e)) from e
    finally:
        try:
            s.close()
        except Exception:
            pass


class RPCError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"rpc {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def try_spawn_daemon() -> bool:
    """fork --detach 静默拉起 daemon；fork 锁忙则放弃。返回是否尝试。"""
    lock = FileLock(LIMEMD_FORK_LOCK_PATH)
    if not lock.acquire():
        return False
    try:
        # 简单非阻塞：spawn limemd --detach
        env = dict(os.environ)
        # 用 sys.executable + -m 确保模块可达
        cmd = [sys.executable, "-m", "limem.daemon.server", "--detach"]
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            return True
        except FileNotFoundError:
            return False
    finally:
        lock.release()


def ensure_or_spawn(*, max_wait_ms: int = 300) -> bool:
    """若 socket 可探活则返回 True；否则尝试拉起并等待至多 max_wait_ms。"""
    try:
        call("_ping", connect_timeout_ms=25, call_timeout_ms=50)
        return True
    except DaemonUnavailable:
        pass
    if not try_spawn_daemon():
        return False
    deadline = time.time() + max_wait_ms / 1000.0
    while time.time() < deadline:
        try:
            call("_ping", connect_timeout_ms=25, call_timeout_ms=50)
            return True
        except DaemonUnavailable:
            time.sleep(0.02)
    return False


# ----- 便利函数 -----


def safe_call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """便利包装：失败返回 None，永不抛 DaemonUnavailable。"""
    try:
        return call(method, params)
    except DaemonUnavailable:
        return None
    except RPCError:
        return None


def get_status() -> dict[str, Any] | None:
    return safe_call("get_status")


def get_status_or_cache() -> dict[str, Any]:
    """先尝试 daemon，失败 fallback 到 cache.json。"""
    r = get_status()
    if r is not None:
        return r
    cache = _read_cache()
    if cache:
        return cache.get("raw", {})
    return {}


def bump_hit(session_id: str) -> None:
    safe_call("bump_hit", {"session_id": session_id})


def set_connectivity(*, status: int = 0, reason: str | None = None, ok: bool = False) -> None:
    safe_call("set_connectivity", {"status": status, "reason": reason, "ok": ok})


def get_connectivity() -> dict[str, Any] | None:
    return safe_call("get_connectivity")


def set_pause(*, duration_seconds: int, scope: str = "project", session_id: str | None = None) -> dict[str, Any] | None:
    return safe_call("set_pause", {
        "duration_seconds": duration_seconds, "scope": scope, "session_id": session_id,
    })


def clear_pause() -> dict[str, Any] | None:
    return safe_call("clear_pause")


def get_pause() -> dict[str, Any] | None:
    return safe_call("get_pause")


def write_memory(params: dict[str, Any]) -> dict[str, Any] | None:
    """daemon 不可达时返回 None；调用方走 fallback。"""
    return safe_call("write_memory", params)


def forget_memory(event_id: str) -> dict[str, Any] | None:
    return safe_call("forget_memory", {"event_id": event_id})


def fix_memory(event_id: str, new_text: str) -> dict[str, Any] | None:
    return safe_call("fix_memory", {"event_id": event_id, "new_text": new_text})


def lookup_short_id(short_id: str) -> str | None:
    r = safe_call("lookup_short_id", {"short_id": short_id})
    return r.get("event_id") if r else None


def auto_init_project(cwd: str) -> dict[str, Any] | None:
    return safe_call("auto_init_project", {"cwd": cwd})


def list_suggestions(status: str = "pending") -> list[dict[str, Any]] | None:
    r = safe_call("list_suggestions", {"status": status})
    return r if isinstance(r, list) else None


def accept_suggestion(sid: str, edited_text: str | None = None, edited_entities: list | None = None) -> dict[str, Any] | None:
    return safe_call("accept_suggestion", {
        "id": sid, "edited_text": edited_text, "edited_entities": edited_entities,
    })


def discard_suggestion(sid: str) -> dict[str, Any] | None:
    return safe_call("discard_suggestion", {"id": sid})


def report_recall(params: dict[str, Any]) -> None:
    """fire-and-forget：daemon 不可达静默；hook 调用永不阻塞。"""
    safe_call("report_recall", params)


def list_recent_recalls(limit: int = 20) -> list[dict[str, Any]] | None:
    """供 dash / CLI；daemon 不可达返回 None，调用方自行读 recent_recalls.json fallback。"""
    r = safe_call("list_recent_recalls", {"limit": limit})
    return r if isinstance(r, list) else None


def seen_recall_keys(session_id: str) -> set[str]:
    """Return memory keys already injected in this session; empty on daemon failure."""
    if not session_id:
        return set()
    r = safe_call("seen_recall_keys", {"session_id": session_id})
    if not isinstance(r, dict):
        return set()
    keys = r.get("keys") or []
    return {str(k) for k in keys if k}


def consume_pending_recall(
    session_id: str, *, dedupe: bool = True
) -> dict[str, Any] | None:
    """Stop hook 用：取出该 session 待消费的最新注入记录，取出后 daemon 即清除。
    daemon 不可达 / 无 pending / 签名去重命中 → 返回 None。
    """
    if not session_id:
        return None
    r = safe_call(
        "consume_pending_recall", {"session_id": session_id, "dedupe": dedupe}
    )
    return r if isinstance(r, dict) else None


def shutdown() -> dict[str, Any] | None:
    return safe_call("shutdown")
