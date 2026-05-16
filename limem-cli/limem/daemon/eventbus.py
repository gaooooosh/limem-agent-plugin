"""events.ndjson tail：daemon 启动期恢复 offset；运行期非阻塞 readline。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from ..config import EVENTS_LOG_PATH, EVENTS_OFFSET_PATH


def emit_event(kind: str, *, tool: str = "", session_id: str = "",
               project_id: str = "", scope: str = "",
               payload: dict[str, Any] | None = None, redacted: bool = False) -> None:
    """供 hook / client 调用：append 一行新 schema 事件到 events.ndjson。

    保证不抛异常（日志失败永不阻塞 hook）。
    """
    try:
        EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "kind": kind,
            "tool": tool,
            "session_id": session_id,
            "project_id": project_id,
            "scope": scope,
            "payload": payload or {},
            "redacted": bool(redacted),
        }
        with EVENTS_LOG_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_offset() -> tuple[int, int]:
    """返回 (inode, offset)；若不存在或解析失败返回 (0, 0)。"""
    try:
        data = json.loads(EVENTS_OFFSET_PATH.read_text())
        return int(data.get("inode", 0)), int(data.get("offset", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0, 0


def _write_offset(inode: int, offset: int) -> None:
    try:
        EVENTS_OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = EVENTS_OFFSET_PATH.with_suffix(EVENTS_OFFSET_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps({"inode": inode, "offset": offset}))
        tmp.replace(EVENTS_OFFSET_PATH)
    except Exception:
        pass


class EventTail:
    """以 inode + offset 跟踪 events.ndjson；inode 变化则从头重读。

    daemon 主循环周期调用 ``poll()``，返回新事件迭代器。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or EVENTS_LOG_PATH
        inode, offset = _read_offset()
        self._last_inode = inode
        self._offset = offset

    def _stat_inode(self) -> int:
        try:
            return os.stat(self.path).st_ino
        except FileNotFoundError:
            return 0

    def poll(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        cur_inode = self._stat_inode()
        # 文件被滚动或首次启动 → 从头读
        if cur_inode != self._last_inode:
            self._last_inode = cur_inode
            self._offset = 0
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                while True:
                    chunk = f.readline()
                    if not chunk:
                        break
                    self._offset = f.tell()
                    line = chunk.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # legacy 行兼容：缺 kind 推断为 legacy
                    if "kind" not in row:
                        row = {
                            "ts": row.get("ts", 0),
                            "kind": "legacy",
                            "tool": row.get("tool", ""),
                            "session_id": "",
                            "project_id": "",
                            "scope": "",
                            "payload": row,
                            "redacted": False,
                        }
                    yield row
            _write_offset(self._last_inode, self._offset)
        except FileNotFoundError:
            return


def rotate_if_needed(*, max_bytes: int, max_age_seconds: int) -> bool:
    """若文件超出阈值则滚动 .1（保留 2 代）。返回是否滚动。"""
    p = EVENTS_LOG_PATH
    if not p.exists():
        return False
    try:
        st = p.stat()
    except FileNotFoundError:
        return False
    too_big = st.st_size > max_bytes
    too_old = (time.time() - st.st_mtime) > max_age_seconds
    if not (too_big or too_old):
        return False
    one = p.with_suffix(p.suffix + ".1")
    two = p.with_suffix(p.suffix + ".2")
    try:
        if one.exists():
            one.replace(two)
        p.replace(one)
    except OSError:
        return False
    # 重置 offset
    _write_offset(0, 0)
    return True
