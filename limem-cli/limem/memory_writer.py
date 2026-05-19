"""四件写入业务逻辑（v3）：``remember`` / ``forget`` / ``fix`` / ``pattern``。

阶段 1 决策：本地 SQLite 写路径统一走 daemon RPC。daemon 不可达时 fallback 到原同步路径。
真正实现在 ``limem.daemon.writer``；本模块是**客户端薄层**。

v3 变化（principal-centric）：
- ``EntitySpec`` 现在表示一个 **mention**（命令 / 路径 / 术语），而**不是后端 entity**。
  字段保留 ``aliases / description / entity_type`` 以兼容旧 MCP/CLI 入参格式。
- ``remember`` 返回 ``RememberResult{event_id, summary, principal_ids, canonicals, scope}``；
  旧字段 ``entity_ids / pattern_count`` 仍保留但语义变为 principal_ids 的镜像。
- ``update_pattern / get_pattern / delete_pattern`` 只接受 principal 的 stable entity_id；
  CLI / MCP 层调用前应已用 ``principals.principal_alias_to_id`` 解析过别名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import daemon_client
from .client import LimemError
from .config import Credentials, RuntimeConfig
from .daemon.writer import (
    delete_pattern_impl,
    fix_impl,
    forget_impl,
    get_pattern_impl,
    remember_impl,
    update_pattern_impl,
)
from .entity_index import EntityIndex


@dataclass
class EntitySpec:
    """一个 mention（命令 / 路径 / 术语）的描述。

    v3：**不再注册为后端 entity**。``aliases`` 进入 BM25 tag-token 与本地
    ``raw_metadata.canonicals`` / ``mentions`` 镜像；``description`` 仅记入本地 mentions
    元信息，不上行。``entity_type`` 保留兼容但 writer 不再消费。
    """

    canonical: str
    role: str = "neutral"
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    entity_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "role": self.role,
            "aliases": list(self.aliases),
            "description": self.description,
            "entity_type": self.entity_type,
        }


@dataclass
class RememberResult:
    """v3 remember 返回值。

    - ``principal_ids``：本次 event 关联的 principal stable entity_ids
    - ``canonicals``：被记录的 mention canonical 列表
    - ``entity_ids`` / ``pattern_count``：v2 字段，等同于 principal_ids；仅为向后兼容
    """

    event_id: str
    summary: str
    scope: str
    principal_ids: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)

    @property
    def entity_ids(self) -> list[str]:
        return list(self.principal_ids)

    @property
    def pattern_count(self) -> int:
        return len(self.principal_ids)


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
    idx: EntityIndex | None = None,
    skip_redact: bool = False,
    prefer_daemon: bool = True,
) -> RememberResult:
    """写入一条新记忆。entities 可空。"""
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
                scope=rpc_result.get("scope", scope),
                principal_ids=list(rpc_result.get("principal_ids") or []),
                canonicals=list(rpc_result.get("canonicals") or []),
            )

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
        idx=idx,
        skip_redact=skip_redact,
    )
    return RememberResult(
        event_id=out["event_id"],
        summary=out["summary"],
        scope=out["scope"],
        principal_ids=list(out.get("principal_ids") or []),
        canonicals=list(out.get("canonicals") or []),
    )


def forget(
    *,
    event_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
    prefer_daemon: bool = True,
) -> dict[str, Any]:
    if prefer_daemon:
        rpc_result = daemon_client.forget_memory(event_id)
        if rpc_result is not None:
            return {
                "backend": {"action": rpc_result.get("backend_action")},
                "local_rows_tombstoned": int(rpc_result.get("local_rows_tombstoned", 0)),
            }
    out = forget_impl(event_id=event_id, creds=creds, idx=idx)
    return {
        "backend": {"action": out.get("backend_action")},
        "local_rows_tombstoned": int(out.get("local_rows_tombstoned", 0)),
    }


def fix(
    *,
    event_id: str,
    new_text: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
    prefer_daemon: bool = True,
) -> dict[str, Any]:
    if prefer_daemon:
        rpc_result = daemon_client.fix_memory(event_id, new_text)
        if rpc_result is not None:
            return rpc_result
    return fix_impl(event_id=event_id, new_text=new_text, creds=creds, idx=idx)


# ---------- v3：principal markdown 档案 ----------


def update_pattern(
    *,
    entity_id: str,
    content: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    """整篇 upsert principal markdown 档案。不走 daemon（操作幂等且短）。"""
    return update_pattern_impl(entity_id=entity_id, content=content, creds=creds, idx=idx)


def get_pattern(
    *,
    entity_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    return get_pattern_impl(entity_id=entity_id, creds=creds, idx=idx)


def delete_pattern(
    *,
    entity_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    return delete_pattern_impl(entity_id=entity_id, creds=creds, idx=idx)


__all__ = [
    "EntitySpec",
    "RememberResult",
    "remember",
    "forget",
    "fix",
    "update_pattern",
    "get_pattern",
    "delete_pattern",
    "LimemError",
]
