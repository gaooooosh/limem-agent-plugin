"""连通性状态机：3 次连续 401/403/5xx → degraded；任意 200 → healthy。"""

from __future__ import annotations

import time

from .state import ConnectivityState

_DEGRADED_THRESHOLD = 3


def record_success(state: ConnectivityState) -> bool:
    """返回是否发生状态切换。"""
    if state.state == "healthy":
        state.consec_fail = 0
        return False
    state.state = "healthy"
    state.reason = None
    state.consec_fail = 0
    state.last_change_ts = int(time.time())
    return True


def record_failure(state: ConnectivityState, *, reason: str) -> bool:
    """累计失败；满阈值切到 degraded。返回是否切换。"""
    state.consec_fail += 1
    if state.state == "degraded":
        # 已 degraded，仅更新 reason
        state.reason = reason
        return False
    if state.consec_fail >= _DEGRADED_THRESHOLD:
        state.state = "degraded"
        state.reason = reason
        state.last_change_ts = int(time.time())
        return True
    return False


def classify_status(status: int) -> str:
    if status == 401:
        return "auth_expired"
    if status == 403:
        return "forbidden"
    if 500 <= status < 600:
        return "server_error"
    if status == 0:
        return "network"
    return "unknown"
