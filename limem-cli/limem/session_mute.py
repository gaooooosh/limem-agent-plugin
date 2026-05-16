"""按 session 屏蔽特定 short_id 的本地状态文件。

存储路径：``~/.cache/limem/session_mute.json``
结构：``{session_id: [short_id1, short_id2, ...]}``
生命周期：SessionEnd / Stop flush 时由 hook 清理本 session 条目。
"""

from __future__ import annotations

import json
from typing import Any

from .config import SESSION_MUTE_PATH


def _read() -> dict[str, list[str]]:
    try:
        data = json.loads(SESSION_MUTE_PATH.read_text())
        if isinstance(data, dict):
            return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _write(data: dict[str, list[str]]) -> None:
    SESSION_MUTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_MUTE_PATH.with_suffix(SESSION_MUTE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(SESSION_MUTE_PATH)


def mute(session_id: str, short_id: str) -> None:
    short_id = (short_id or "").lstrip("#").strip()
    if not session_id or not short_id:
        return
    data = _read()
    items = data.setdefault(session_id, [])
    if short_id not in items:
        items.append(short_id)
    _write(data)


def is_muted(session_id: str, short_id: str) -> bool:
    if not session_id or not short_id:
        return False
    short_id = short_id.lstrip("#").strip()
    return short_id in _read().get(session_id, [])


def get_muted(session_id: str) -> set[str]:
    return set(_read().get(session_id, []))


def clear(session_id: str) -> None:
    data = _read()
    if session_id in data:
        del data[session_id]
        _write(data)
