"""Daemon 内独占的 SQLite 写者：remember / forget / fix / pattern。

memory_writer.py 的客户端薄层若 daemon 可达则全部走 RPC 到这里；不可达 fallback 直接调本模块。

v3 设计要点（principal-centric）：
- ``remember_impl`` 只负责 **event ingest**；不再为 entities 参数里的 canonical 注册后端
  entity，也不再写本地 ``entities`` 表。
- ``entities`` 入参降级为 mention 元信息：写入 ``raw_metadata.canonicals`` 与
  ``[limem.canonical=...]`` tag token；mentions 在 event 文本里同样保留。
- ``active_principal_ids`` 由 scope / mem_type 推断，写入 ``raw_metadata.principal_ids``
  作为 soft recall 二次过滤依据。``ensure_default_principals`` 在写入前 lazy 注册。
- ``update_pattern_impl / get_pattern_impl / delete_pattern_impl`` 仅接受 principal 的
  stable entity_id（或上层已经 alias_to_id 解析后的 id）。
- ``patterns_indexed`` 字段语义变更：现在表示 ``principal_ids`` 数量。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..client import LimemClient, LimemError, PatternRecallResult
from ..config import Credentials, RuntimeConfig
from ..entity_index import EntityIndex
from ..principals import ensure_default_principals, normalize_project_deictics
from ..redact import contains_secret
from ..tag_text import encode_tags


def build_natural_detail(
    *,
    text: str,
    detail: str = "",
    scope: str = "",
    mem_type: str = "",
    project_id: str = "",
    session_id: str = "",
    source: str = "",
    timestamp: int | None = None,
) -> str:
    """Build a human-readable detail field for LiMem ingest.

    The service consumes ``detail`` as natural context, so keep it prose-like
    while still including enough provenance to understand the event later.
    """
    ts = timestamp if timestamp is not None else int(time.time())
    time_text = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    project = project_id or (scope.split(":", 1)[1] if scope.startswith("project:") else "")
    scope_desc = scope or "unknown"
    source_desc = source or "unknown"
    type_desc = mem_type or "memory"
    content = (detail or text or "").strip()

    context_parts = [f"时间为 {time_text}"]
    if project:
        context_parts.append(f"当前项目是 {project}")
    context_parts.append(f"记忆范围是 {scope_desc}")
    if session_id:
        context_parts.append(f"会话是 {session_id}")
    context_parts.append(f"工具来源是 {source_desc}")

    body = f"具体发生的内容是：{content}" if content else "具体发生的内容暂未提供。"
    return f"现在的情况是：{'，'.join(context_parts)}。这是一条 {type_desc} 类型的 LiMem 记忆。{body}"


def _infer_principal_ids(
    *,
    scope: str,
    mem_type: str,
    creds: Credentials | None,
    project_id: str,
    session_id: str,
    idx: EntityIndex,
) -> list[str]:
    """根据 scope / mem_type 推断本次 event 应挂的 principals（stable entity_id）。"""
    from ..principals import PrincipalSpec, entity_id_for

    user_id = (creds.user_id if creds else "") or ""
    out: list[str] = []
    seen: set[str] = set()

    def _push(eid: str) -> None:
        if eid and eid not in seen:
            seen.add(eid)
            out.append(eid)

    proj_id = ""
    if scope.startswith("project:"):
        proj_id = scope.split(":", 1)[1] or project_id
    else:
        proj_id = project_id

    if scope.startswith("project:") and proj_id:
        _push(entity_id_for(PrincipalSpec("project", slug=proj_id, project_id=proj_id, description="")))

    if scope == "global" and mem_type in {"rule", "preference", "fact"}:
        if user_id:
            _push(entity_id_for(PrincipalSpec("user", slug=user_id, description="")))

    if mem_type == "feedback":
        if user_id:
            _push(entity_id_for(PrincipalSpec("user", slug=user_id, description="")))
        # agent principal 由 session_id 或本地 active principals 决定
        try:
            agents = idx.list_principals(active_only=True, principal_types=["agent"])
            for p in agents:
                _push(p.entity_id)
        except Exception:
            pass

    if scope.startswith("session:"):
        try:
            agents = idx.list_principals(active_only=True, principal_types=["agent"])
            for p in agents:
                _push(p.entity_id)
        except Exception:
            pass
        if proj_id:
            _push(entity_id_for(PrincipalSpec("project", slug=proj_id, project_id=proj_id, description="")))

    # session_id 当前仅做日志参考，不再独立映射为 principal
    _ = session_id
    return out


def remember_impl(
    *,
    text: str,
    scope: str,
    mem_type: str = "rule",
    importance: float = 0.9,
    project_id: str = "",
    entities: list[dict[str, Any]] | None = None,
    source: str = "limem-cli",
    session_id: str = "",
    detail: str = "",
    creds: Credentials | None = None,
    runtime: RuntimeConfig | None = None,
    idx: EntityIndex | None = None,
    skip_redact: bool = False,
) -> dict[str, Any]:
    """写入一条新记忆。

    ``entities`` 形如 ``[{canonical, role, aliases?, description?, entity_type?}]``。
    v3：**不再注册后端 entity**；canonical 仅作 mention 写入 event 文本与本地 metadata。
    若需为 user / agent / project 沉淀长期档案，使用 ``/limem.pattern``。
    """
    creds = creds or Credentials.load()
    runtime = runtime or RuntimeConfig.load()
    idx = idx or EntityIndex()

    if not skip_redact:
        hit = contains_secret(text, runtime.redact_patterns)
        if hit:
            raise ValueError(f"redact: looks like a secret token: {hit}")

    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")

    # 指代词归一化：仅在 scope 已锁定 project 时启用。
    # global / session: scope 下用户讲"本项目"通常是抽象修饰，无明确 project context
    # 不可贸然替换。同步覆盖 text / detail，让 composed_text、BM25 索引、
    # raw_metadata.original_text 三处保持一致；build_natural_detail 自带的 provenance
    # 句不参与归一化。
    if scope.startswith("project:"):
        effective_proj_id = scope.split(":", 1)[1] or project_id
        if effective_proj_id:
            text = normalize_project_deictics(text, project_id=effective_proj_id)
            if detail:
                detail = normalize_project_deictics(detail, project_id=effective_proj_id)

    canonicals = [e["canonical"] for e in entities or [] if e.get("canonical")]

    # principals 推断 + lazy ensure（agent tool 在 daemon 路径未知，跳过；hook 路径会确保）
    principal_ids = _infer_principal_ids(
        scope=scope,
        mem_type=mem_type,
        creds=creds,
        project_id=project_id,
        session_id=session_id,
        idx=idx,
    )
    try:
        client = LimemClient(creds=creds)
        ensure_default_principals(
            creds,
            project_id=project_id,
            tool="",  # writer 不知 tool；hook 已在 SessionStart 注册过 agent
            idx=idx,
            client=client,
        )
    except Exception:
        client = LimemClient(creds=creds)

    tag_block = encode_tags(
        scope=scope,
        type=mem_type,
        canonical=canonicals or None,
        principal=principal_ids or None,
    )
    composed_text = f"{tag_block} {text}".strip()

    ts = int(time.time())
    data = {
        "source": source,
        "limem_scope": scope,
        "limem_type": mem_type,
        "project_id": project_id,
        "session_id": session_id,
        "importance": importance,
        "text": composed_text,
        "detail": build_natural_detail(
            text=text,
            detail=detail,
            scope=scope,
            mem_type=mem_type,
            project_id=project_id,
            session_id=session_id,
            source=source,
            timestamp=ts,
        ),
    }
    ingest_res = client.ingest(data, timestamp=ts)
    event_id = ingest_res.event_id

    idx.upsert_event_metadata(
        {
            "event_id": event_id,
            "scope": scope,
            "mem_type": mem_type,
            "project_id": project_id,
            "importance": importance,
            "role": (entities or [{}])[0].get("role", "") if entities else "",
            "source": source,
            "ts": ts,
            "summary": ingest_res.summary or text[:200],
            "raw_metadata": {
                "canonicals": canonicals,
                "original_text": text,
                "principal_ids": principal_ids,
                "mentions": [
                    {
                        "canonical": e.get("canonical"),
                        "role": e.get("role", "neutral"),
                        "aliases": list(e.get("aliases") or []),
                        "description": e.get("description") or "",
                    }
                    for e in entities or []
                    if e.get("canonical")
                ],
            },
        }
    )

    try:
        idx.ensure_short_id(event_id)
    except Exception:
        pass

    return {
        "event_id": event_id,
        "scope": scope,
        "summary": ingest_res.summary or text[:200],
        "principal_ids": principal_ids,
        "canonicals": canonicals,
        # 旧字段保留兼容 RPC 调用方；v3 语义已无 entity 注册行为
        "entities_registered": [],
        "patterns_indexed": len(principal_ids),
    }


def forget_impl(
    *,
    event_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    client = LimemClient(creds=creds)
    backend_res = client.graph_archive_event(event_id)
    local_rows = idx.tombstone_event(event_id)
    return {
        "event_id": event_id,
        "backend_action": backend_res.get("action", "archive"),
        "local_rows_tombstoned": local_rows,
    }


def fix_impl(
    *,
    event_id: str,
    new_text: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    """更新一个 event 的 original_text/summary（**不动 principal pattern markdown**）。

    v3 语义：``/limem.fix`` 只修订 event 文本；要改 principal 档案请走 ``/limem.pattern``。
    """
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    client = LimemClient(creds=creds)
    fields = {"original_text": new_text, "summary": new_text[:200]}
    client.graph_update_event(event_id, fields)
    meta = idx.lookup_event(event_id)
    if meta is not None:
        raw = dict(meta.raw_metadata or {})
        raw["original_text"] = new_text
        idx.upsert_event_metadata(
            {
                "event_id": event_id,
                "scope": meta.scope,
                "mem_type": meta.mem_type,
                "project_id": meta.project_id,
                "importance": meta.importance,
                "role": meta.role,
                "source": meta.source,
                "ts": meta.ts,
                "summary": new_text[:200],
                "raw_metadata": raw,
            }
        )
    return {"event_id": event_id, "updated": True}


def update_pattern_impl(
    *,
    entity_id: str,
    content: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    """整篇 upsert principal markdown，同步本地 has_pattern=1。"""
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")
    client = LimemClient(creds=creds)
    action, pattern = client.patterns_upsert(entity_id, content)
    idx.mark_principal_has_pattern(entity_id, has_pattern=True)
    return {
        "entity_id": entity_id,
        "action": action,
        "pattern_id": pattern.pattern_id if pattern else "",
        "total_chars": len(content),
    }


def get_pattern_impl(
    *,
    entity_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")
    client = LimemClient(creds=creds)
    res: PatternRecallResult = client.patterns_get(entity_id)
    idx.mark_principal_has_pattern(entity_id, has_pattern=res.has_content())
    return {
        "entity_id": entity_id,
        "mode": res.mode,
        "content": res.content,
        "total_chars": res.total_chars,
        "has_pattern": res.has_content(),
    }


def delete_pattern_impl(
    *,
    entity_id: str,
    creds: Credentials | None = None,
    idx: EntityIndex | None = None,
) -> dict[str, Any]:
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")
    client = LimemClient(creds=creds)
    try:
        snapshot = client.patterns_delete(entity_id)
        deleted = True
    except LimemError as e:
        if e.status == 404:
            snapshot = None
            deleted = False
        else:
            raise
    idx.mark_principal_has_pattern(entity_id, has_pattern=False)
    return {
        "entity_id": entity_id,
        "deleted": deleted,
        "pattern_id": snapshot.pattern_id if snapshot else "",
    }
