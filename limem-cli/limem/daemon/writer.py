"""Daemon 内独占的 SQLite 写者：remember / forget / fix 的实际实现。

memory_writer.py 的客户端薄层若 daemon 可达则全部走 RPC 到这里；不可达 fallback 直接调本模块。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..client import LimemClient, LimemError
from ..config import Credentials, RuntimeConfig
from ..pattern_index import PatternIndex
from ..redact import contains_secret
from ..tag_text import encode_tags


def _slugify(name: str, max_len: int = 32) -> str:
    import re
    s = re.sub(r"[^\w]+", "_", name.lower())
    return s.strip("_")[:max_len] or "x"


def _stable_entity_id(canonical: str, role: str, scope: str) -> str:
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
    pidx: PatternIndex | None = None,
    skip_redact: bool = False,
) -> dict[str, Any]:
    """写入一条新记忆。entities 形如 [{canonical, role, patterns: [..]}]。"""
    creds = creds or Credentials.load()
    runtime = runtime or RuntimeConfig.load()
    pidx = pidx or PatternIndex()

    if not skip_redact:
        hit = contains_secret(text, runtime.redact_patterns)
        if hit:
            raise ValueError(f"redact: looks like a secret token: {hit}")

    if not creds.api_key or not creds.db_id:
        raise LimemError(0, "missing credentials: run `limem bootstrap` or `limem init` first")

    client = LimemClient(creds=creds)

    canonicals = [e["canonical"] for e in entities or []]
    all_patterns = [p for e in entities or [] for p in e.get("patterns", [])]
    tag_block = encode_tags(
        scope=scope,
        type=mem_type,
        canonical=canonicals or None,
        patterns=all_patterns or None,
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

    pidx.upsert_event_metadata(
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
    pattern_count = 0
    for ent in entities or []:
        canonical = ent["canonical"]
        role = ent.get("role", "neutral")
        patterns = list(ent.get("patterns") or [])
        eid = _stable_entity_id(canonical, role, scope)
        try:
            client.register_entity(
                entity_id=eid,
                description=f"{canonical}（{role}）— 来自规则 {event_id[:12]}",
                entity_type=f"{mem_type}_entity",
                aliases=[canonical],
                metadata={
                    "limem_scope": scope,
                    "linked_event_id": event_id,
                    "role": role,
                },
                patterns=[{"content": p, "pattern_type": "trigger"} for p in patterns],
            )
            entity_ids.append(eid)
        except LimemError as e:
            if e.status == 409:
                try:
                    client.batch_create_entity_patterns(
                        eid,
                        [{"content": p, "pattern_type": "trigger"} for p in patterns],
                    )
                    entity_ids.append(eid)
                except LimemError:
                    continue
            else:
                raise

        pattern_rows = []
        for p in patterns:
            pid = f"{eid}__{hashlib.sha1(p.encode()).hexdigest()[:10]}"
            pattern_rows.append(
                {
                    "pattern_id": pid,
                    "content": p,
                    "entity_id": eid,
                    "event_id": event_id,
                    "scope": scope,
                    "role": role,
                    "importance": importance,
                    "ts": ts,
                }
            )
        if pattern_rows:
            pattern_count += pidx.upsert_patterns(pattern_rows)

    # short_id 生成（阶段 4 表，daemon 启动后必存在）
    try:
        pidx.ensure_short_id(event_id)
    except Exception:
        pass

    return {
        "event_id": event_id,
        "scope": scope,
        "summary": ingest_res.summary or text[:200],
        "entities_registered": entity_ids,
        "patterns_indexed": pattern_count,
    }


def forget_impl(
    *,
    event_id: str,
    creds: Credentials | None = None,
    pidx: PatternIndex | None = None,
) -> dict[str, Any]:
    creds = creds or Credentials.load()
    pidx = pidx or PatternIndex()
    client = LimemClient(creds=creds)
    backend_res = client.graph_archive_event(event_id)
    local_rows = pidx.tombstone_event(event_id)
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
    pidx: PatternIndex | None = None,
) -> dict[str, Any]:
    """更新一个 event 的 original_text/summary。禁止生成新 event_id。"""
    creds = creds or Credentials.load()
    pidx = pidx or PatternIndex()
    client = LimemClient(creds=creds)
    fields = {"original_text": new_text, "summary": new_text[:200]}
    client.graph_update_event(event_id, fields)
    meta = pidx.lookup_event(event_id)
    if meta is not None:
        raw = dict(meta.raw_metadata or {})
        raw["original_text"] = new_text
        pidx.upsert_event_metadata(
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
