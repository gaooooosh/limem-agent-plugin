"""Line-delimited JSON-RPC 2.0（最小子集）。

约定：每条请求/响应单行 JSON + ``\\n``，UTF-8 编码。错误码：
- 标准：-32600 invalid request / -32601 method not found / -32602 invalid params / -32603 internal
- 自定：1001 paused / 1002 degraded / 1003 not_found_short_id / 1004 dirty_repo / 1005 daemon_unavailable
"""

from __future__ import annotations

import json
from typing import Any

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

PAUSED = 1001
DEGRADED = 1002
NOT_FOUND_SHORT_ID = 1003
DIRTY_REPO = 1004
DAEMON_UNAVAILABLE = 1005


def make_request(method: str, params: dict[str, Any] | None = None, *, req_id: int = 1) -> bytes:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def make_result(req_id: Any, result: Any) -> bytes:
    body = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def make_error(req_id: Any, code: int, message: str, data: Any = None) -> bytes:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    body = {"jsonrpc": "2.0", "id": req_id, "error": err}
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def parse_line(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
