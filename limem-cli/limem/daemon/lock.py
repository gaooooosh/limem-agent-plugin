"""Daemon 单实例锁与 fork 锁。"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


class FileLock:
    """非阻塞 flock 包装。``acquire()`` 失败返回 False（即"已被其他进程持有"）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self) -> bool:
        self._fh = open(self.path, "ab+")  # noqa: SIM115
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise BlockingIOError(f"lock held: {self.path}")
        return self

    def __exit__(self, *_a: object) -> None:
        self.release()


def write_pid(path: Path, pid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(pid or os.getpid()))
    tmp.replace(path)


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权 signal — 仍算 alive
        return True
    except OSError:
        return False
