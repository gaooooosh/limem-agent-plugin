"""四件写入业务逻辑（v2）：``remember`` / ``forget`` / ``fix`` / ``pattern``。

阶段 1 决策：本地 SQLite 写路径统一走 daemon RPC。daemon 不可达时 fallback 到原同步路径。
真正实现在 ``limem.daemon.writer``；本模块是**客户端薄层**。

v2 变化（决策 3）：
- ``EntitySpec`` 移除 ``patterns: list[str]``（trigger 短语数组已下线）；改成可选 ``aliases``/``description``。
- 新增 ``update_pattern`` / ``get_pattern`` / ``delete_pattern``——entity markdown 档案的唯一写入入口
  （供新 skill ``/limem.pattern`` 与 MCP 工具 ``limem_pattern_put`` 使用）。
- ``fix`` 语义不变：只改 event 文本；要改 entity 档案请走 pattern API。
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
    """注册到后端的 entity 描述。

    v2：不再包含 trigger 短语数组；aliases/description 是可选元信息。
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
    event_id: str
    summary: str
    entity_ids: list[str]
    pattern_count: int  # 含义：本次镜像到本地 entity_index 的实体数（保留旧字段名）
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
                entity_ids=list(rpc_result.get("entities_registered") or []),
                pattern_count=int(rpc_result.get("patterns_indexed", 0)),
                scope=rpc_result.get("scope", scope),
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
        entity_ids=list(out["entities_registered"]),
        pattern_count=int(out["patterns_indexed"]),
        scope=out["scope"],
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


# ---------- v2 新增：entity markdown 档案 ----------


def update_pattern(
    *,
    entity_id: str,
    content: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    """整篇 upsert entity markdown 档案。不走 daemon（操作幂等且短）。"""
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


# 暴露 LimemError 以便 skill / CLI 捕获时无需多 import
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
