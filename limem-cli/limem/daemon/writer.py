"""Daemon 内独占的 SQLite 写者：remember / forget / fix / pattern。

memory_writer.py 的客户端薄层若 daemon 可达则全部走 RPC 到这里；不可达 fallback 直接调本模块。

v2 设计要点：
- ``remember_impl`` 只负责 **event ingest + entity 注册（或晋升）**；**不再内联 pattern markdown**。
- ``update_pattern_impl`` 是 entity markdown 的唯一写入入口（决策 3：/limem.pattern skill）。
- 老的 ``patterns_indexed`` 字段语义改为"本次被刷新到本地 entity_index 的实体数"。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from ..client import LimemClient, LimemError, PatternRecallResult
from ..config import Credentials, RuntimeConfig
from ..entity_index import EntityIndex
from ..redact import contains_secret
from ..tag_text import encode_tags


def _slugify(name: str, max_len: int = 32) -> str:
    s = re.sub(r"[^\w]+", "_", name.lower())
    return s.strip("_")[:max_len] or "x"


def _stable_entity_id(canonical: str, role: str, scope: str) -> str:
    """生成稳定的 entity_id（content-addressed），保持 v1 兼容以避免后端 entity 出现孤儿。"""
    base = f"{canonical}|{role}|{scope}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{_slugify(canonical)}_{role}_{h}"


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
    与 v1 区别：**不再接 ``patterns`` 字段**；markdown 走 /limem.pattern 独立路径。
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

    client = LimemClient(creds=creds)

    canonicals = [e["canonical"] for e in entities or []]
    # tag-as-token 仅在写入端编入 BM25 文本（query 端仍不接 filters，召回后客户端二次过滤）
    tag_block = encode_tags(
        scope=scope,
        type=mem_type,
        canonical=canonicals or None,
        patterns=None,  # v2：trigger 短语已下线
    )
    composed_text = f"{tag_block} {text}".strip()

    data = {
        "source": source,
        "limem_scope": scope,
        "limem_type": mem_type,
        "project_id": project_id,
        "session_id": session_id,
        "importance": importance,
        "text": composed_text,
        "detail": detail or text,
    }
    ts = int(time.time())
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
            "raw_metadata": {"canonicals": canonicals, "original_text": text},
        }
    )

    entity_ids: list[str] = []
    entities_indexed = 0
    for ent in entities or []:
        canonical = ent["canonical"]
        role = ent.get("role", "neutral")
        aliases = list(ent.get("aliases") or [])
        description = ent.get("description") or f"{canonical}（{role}）— 来自规则 {event_id[:12]}"
        entity_type = ent.get("entity_type") or f"{mem_type}_entity"
        eid = _stable_entity_id(canonical, role, scope)

        # 别名集合：canonical 一定进 aliases（便于 FTS 反查）
        merged_aliases = list({*aliases, canonical})

        backend_pattern_present = False
        try:
            res = client.entity_create_or_promote(
                eid,
                description,
                entity_type=entity_type,
                aliases=merged_aliases,
                metadata={
                    "limem_scope": scope,
                    "linked_event_id": event_id,
                    "role": role,
                },
            )
            entity_ids.append(eid)
            backend_pattern_present = res.pattern is not None
        except LimemError as e:
            if e.status == 409:
                # 已存在：用 PATCH 增量更新别名（覆盖描述要慎重，保留旧的）
                try:
                    client.entity_patch(
                        eid,
                        add_aliases=merged_aliases,
                        metadata={
                            "limem_scope": scope,
                            "linked_event_id": event_id,
                            "role": role,
                        },
                    )
                    entity_ids.append(eid)
                except LimemError:
                    continue
            else:
                raise

        idx.upsert_entity(
            entity_id=eid,
            canonical=canonical,
            aliases=merged_aliases,
            description=description,
            entity_type=entity_type,
            scope=scope,
            role=role,
            importance=importance,
            has_pattern=backend_pattern_present,
            raw_metadata={"linked_event_id": event_id},
            ts=ts,
        )
        entities_indexed += 1

    try:
        idx.ensure_short_id(event_id)
    except Exception:
        pass

    return {
        "event_id": event_id,
        "scope": scope,
        "summary": ingest_res.summary or text[:200],
        "entities_registered": entity_ids,
        "patterns_indexed": entities_indexed,  # 保留字段名以兼容 RPC 返回结构
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
    """更新一个 event 的 original_text/summary（**不动 entity pattern markdown**）。

    决策 3 后语义：``/limem.fix`` 只修订 event 文本；要改 entity 档案请走 ``/limem.pattern``。
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
    """整篇 upsert entity markdown，同步本地 has_pattern=1。"""
    creds = creds or Credentials.load()
    idx = idx or EntityIndex()
    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")
    client = LimemClient(creds=creds)
    action, pattern = client.patterns_upsert(entity_id, content)
    idx.mark_has_pattern(entity_id, has_pattern=True)
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
    # 同步 has_pattern 本地标记
    idx.mark_has_pattern(entity_id, has_pattern=res.has_content())
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
    idx.mark_has_pattern(entity_id, has_pattern=False)
    return {
        "entity_id": entity_id,
        "deleted": deleted,
        "pattern_id": snapshot.pattern_id if snapshot else "",
    }
