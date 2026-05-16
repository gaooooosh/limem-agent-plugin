"""跨平台系统通知（best-effort）：notify-send → osascript → 静默。"""

from __future__ import annotations

import shutil
import subprocess


def notify(title: str, message: str) -> bool:
    """返回是否成功发出通知（任何一个 channel 成功即 True）。"""
    # Linux
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", title, message],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # macOS
    if shutil.which("osascript"):
        try:
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(
                ["osascript", "-e", script],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return False
