"""F1 自动 project init：daemon RPC 实现。

行为：
1. cwd 必须是 git 仓库（缓存 30s）
2. .limem/local.json 存在 → skipped: exists
3. git status --porcelain 非空 → skipped: dirty，daemon 写 init_pending 标志位 5 分钟
4. clean → 写两文件 + 发系统通知 + 写 inited_now_ts
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..scope import detect_project_id

_GIT_STATUS_CACHE: dict[str, tuple[float, bool]] = {}
_GIT_STATUS_TTL = 30.0


def _is_git_repo(cwd: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=1,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_dirty(cwd: Path) -> bool:
    key = str(cwd.resolve())
    now = time.time()
    cached = _GIT_STATUS_CACHE.get(key)
    if cached and now - cached[0] < _GIT_STATUS_TTL:
        return cached[1]
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        )
        dirty = bool(out.stdout.strip()) if out.returncode == 0 else True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        dirty = True
    _GIT_STATUS_CACHE[key] = (now, dirty)
    return dirty


def auto_init(cwd: str) -> dict[str, Any]:
    """返回 {created: bool, skipped_reason: str|None, project_id: str|None}"""
    root = Path(cwd).resolve()
    if not _is_git_repo(root):
        return {"created": False, "skipped_reason": "not_git", "project_id": None}

    local_path = root / ".limem" / "local.json"
    if local_path.exists():
        return {"created": False, "skipped_reason": "exists", "project_id": None}

    if _is_dirty(root):
        return {"created": False, "skipped_reason": "dirty", "project_id": None}

    pid = detect_project_id(root)
    local_path.parent.mkdir(exist_ok=True)
    local_path.write_text(json.dumps({"project_id": pid, "enabled_hooks": []}, indent=2, ensure_ascii=False))

    gitignore = root / ".gitignore"
    line = ".limem/local.json"
    if gitignore.exists():
        existing = gitignore.read_text()
        if line not in existing.splitlines():
            with gitignore.open("a") as f:
                if not existing.endswith("\n"):
                    f.write("\n")
                f.write(line + "\n")
    else:
        gitignore.write_text(line + "\n")

    # 系统通知（best-effort）
    try:
        from ..notify import notify
        notify("LiMem", f"项目记忆已自动启用：{pid}")
    except Exception:
        pass

    return {"created": True, "skipped_reason": None, "project_id": pid}
