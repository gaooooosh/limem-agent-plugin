"""termios raw mode 非阻塞按键读取。"""

from __future__ import annotations

import select
import sys
import termios
import tty
from contextlib import contextmanager


@contextmanager
def raw_mode():
    """临时进入 termios cbreak（保留信号字符），退出时恢复。"""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_key(timeout: float = 0.5) -> str | None:
    """非阻塞读单字符。无输入返回 None。"""
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    try:
        ch = sys.stdin.read(1)
    except Exception:
        return None
    return ch or None
