"""三件写入业务逻辑：``remember`` / ``forget`` / ``fix``。

阶段 1 决策：本地 SQLite 写路径统一走 daemon RPC。daemon 不可达时 fallback 到原同步路径。
真正实现在 ``limem.daemon.writer``；本模块是**客户端薄层**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import daemon_client
from .client import LimemError
from .config import Credentials, RuntimeConfig
from .daemon.writer import fix_impl, forget_impl, remember_impl
from .pattern_index import PatternIndex


@dataclass
class EntitySpec:
    canonical: str
    role: str
    patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"canonical": self.canonical, "role": self.role, "patterns": list(self.patterns)}


@dataclass
class RememberResult:
    event_id: str
    summary: str
    entity_ids: list[str]
    pattern_count: int
    scope: str


def remember(
    *,
    text: str,
    scope: str,
    mem_type: str = "rule",
    importance: float = 0.9,
    project_id: str = "",
    entities: list[EntitySpec] | None = None,
    source: str = "limem-cli",
    session_id: str = "",
    detail: str = "",
    creds: Credentials | None = None,
    runtime: RuntimeConfig | None = None,
    pidx: PatternIndex | None = None,
    skip_redact: bool = False,
    prefer_daemon: bool = True,
) -> RememberResult:
    """写入一条新记忆。entities 可空。

    daemon 可达时调 RPC ``write_memory``；不可达 fallback 到原同步逻辑。
    """
    ents = [e.to_dict() for e in entities or []]

    if prefer_daemon:
        rpc_result = daemon_client.write_memory(
            {
                "text": text,
                "scope": scope,
                "mem_type": mem_type,
                "importance": importance,
                "project_id": project_id,
                "entities": ents or None,
                "source": source,
                "session_id": session_id,
                "detail": detail,
                "skip_redact": skip_redact,
            }
        )
        if rpc_result is not None:
            return RememberResult(
                event_id=rpc_result.get("event_id", ""),
                summary=rpc_result.get("summary", ""),
                entity_ids=list(rpc_result.get("entities_registered") or []),
                pattern_count=int(rpc_result.get("patterns_indexed", 0)),
                scope=rpc_result.get("scope", scope),
            )

    # fallback：daemon 不可达，直接走本地实现
    out = remember_impl(
        text=text,
        scope=scope,
        mem_type=mem_type,
        importance=importance,
        project_id=project_id,
        entities=ents or None,
        source=source,
        session_id=session_id,
        detail=detail,
        creds=creds,
        runtime=runtime,
        pidx=pidx,
        skip_redact=skip_redact,
    )
    return RememberResult(
        event_id=out["event_id"],
        summary=out["summary"],
        entity_ids=list(out["entities_registered"]),
        pattern_count=int(out["patterns_indexed"]),
        scope=out["scope"],
    )


def forget(
    *,
    event_id: str,
    creds: Credentials | None = None,
    pidx: PatternIndex | None = None,
    prefer_daemon: bool = True,
) -> dict[str, Any]:
    if prefer_daemon:
        rpc_result = daemon_client.forget_memory(event_id)
        if rpc_result is not None:
            return {
                "backend": {"action": rpc_result.get("backend_action")},
                "local_rows_tombstoned": int(rpc_result.get("local_rows_tombstoned", 0)),
            }
    out = forget_impl(event_id=event_id, creds=creds, pidx=pidx)
    return {
        "backend": {"action": out.get("backend_action")},
        "local_rows_tombstoned": int(out.get("local_rows_tombstoned", 0)),
    }


def fix(
    *,
    event_id: str,
    new_text: str,
    creds: Credentials | None = None,
    pidx: PatternIndex | None = None,
    prefer_daemon: bool = True,
) -> dict[str, Any]:
    if prefer_daemon:
        rpc_result = daemon_client.fix_memory(event_id, new_text)
        if rpc_result is not None:
            return rpc_result
    return fix_impl(event_id=event_id, new_text=new_text, creds=creds, pidx=pidx)
