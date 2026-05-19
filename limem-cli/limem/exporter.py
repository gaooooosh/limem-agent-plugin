"""``limem export`` — 本地全量导出（JSON / Markdown）。

v2 schema：导出 events + 关联 entities + 每个 entity 的 markdown 档案。
- events 来自本地 ``event_metadata`` 镜像。
- entities 来自本地 ``entities`` 表。
- patterns（markdown）按需调 ``client.patterns_get`` 拉取（每个 entity 至多 1 篇）。

约束：不调 ``/query`` 端点（避免污染缓存与 BM25 stats）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .client import LimemClient, LimemError
from .config import Credentials
from .entity_index import EntityIndex


def _collect_events(*, include_tombstoned: bool) -> list[dict[str, Any]]:
    idx = EntityIndex()
    out: list[dict[str, Any]] = []
    for meta in idx.iter_all_events(include_tombstoned=include_tombstoned):
        out.append(
            {
                "event_id": meta.event_id,
                "scope": meta.scope,
                "mem_type": meta.mem_type,
                "project_id": meta.project_id,
                "importance": meta.importance,
                "role": meta.role,
                "source": meta.source,
                "ts": meta.ts,
                "summary": meta.summary,
                "original_text": (meta.raw_metadata or {}).get("original_text", ""),
                "canonicals": (meta.raw_metadata or {}).get("canonicals", []),
            }
        )
    return out


def _collect_entities() -> list[dict[str, Any]]:
    idx = EntityIndex()
    out: list[dict[str, Any]] = []
    with idx._conn() as conn:  # noqa: SLF001 (导出场景借用内部连接)
        rows = conn.execute(
            "SELECT entity_id, canonical, aliases, description, entity_type, "
            "       scope, role, importance, has_pattern, last_seen_ts "
            "FROM entities WHERE tombstone=0 ORDER BY scope, canonical"
        ).fetchall()
    for r in rows:
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except (json.JSONDecodeError, TypeError):
            aliases = []
        out.append(
            {
                "entity_id": r["entity_id"],
                "canonical": r["canonical"],
                "aliases": aliases if isinstance(aliases, list) else [],
                "description": r["description"] or "",
                "entity_type": r["entity_type"] or "",
                "scope": r["scope"],
                "role": r["role"] or "",
                "importance": float(r["importance"] or 0.0),
                "has_pattern": bool(r["has_pattern"]),
                "last_seen_ts": int(r["last_seen_ts"] or 0),
                "pattern_markdown": "",
            }
        )
    return out


def _fill_patterns(entities: list[dict[str, Any]], creds: Credentials) -> None:
    if not creds.api_key or not creds.db_id:
        return
    client = LimemClient(creds=creds)
    for ent in entities:
        if not ent.get("has_pattern"):
            continue
        try:
            res = client.patterns_get(ent["entity_id"])
            ent["pattern_markdown"] = res.content
        except (LimemError, sqlite3.Error):
            continue
        except Exception:
            continue


def export(
    *,
    fmt: str = "json",
    output: Path | None = None,
    include_tombstoned: bool = False,
    fill_patterns: bool = True,
) -> Path:
    """fmt: ``json`` | ``markdown``；返回写入的文件路径（chmod 600）。"""
    events = _collect_events(include_tombstoned=include_tombstoned)
    entities = _collect_entities()
    if fill_patterns:
        try:
            _fill_patterns(entities, Credentials.load())
        except Exception:
            pass

    ts = int(time.time())
    if output is None:
        suffix = "json" if fmt == "json" else "md"
        output = Path.cwd() / f"limem-export-{ts}.{suffix}"
    output = output.resolve()

    if fmt == "json":
        body = {
            "export_ts": ts,
            "schema_version": 2,
            "events": events,
            "entities": entities,
        }
        output.write_text(json.dumps(body, ensure_ascii=False, indent=2))
    elif fmt == "markdown":
        output.write_text(_to_markdown(events, entities))
    else:
        raise ValueError(f"unknown format: {fmt}")

    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    return output


def _to_markdown(events: list[dict[str, Any]], entities: list[dict[str, Any]]) -> str:
    lines: list[str] = ["# LiMem export", ""]

    # 1) events 按 scope/type 分组
    lines.append("## Events")
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for it in events:
        by_scope.setdefault(it["scope"], []).append(it)
    for scope, scoped in sorted(by_scope.items()):
        lines.append(f"### scope: {scope}")
        by_type: dict[str, list[dict[str, Any]]] = {}
        for it in scoped:
            by_type.setdefault(it["mem_type"], []).append(it)
        for mt, mt_items in sorted(by_type.items()):
            lines.append(f"#### {mt}")
            for it in mt_items:
                short = it["event_id"][:12]
                text = it.get("original_text") or it.get("summary") or ""
                lines.append(f"- [#{short}] {text} (importance={it['importance']:.2f})")
            lines.append("")

    # 2) entities + 档案
    lines.append("## Entities")
    for ent in entities:
        lines.append(f"### {ent['canonical']}  `{ent['entity_id']}`")
        lines.append(f"- scope: `{ent['scope']}`  role: `{ent['role']}`  type: `{ent['entity_type']}`")
        if ent["aliases"]:
            lines.append(f"- aliases: {', '.join(ent['aliases'])}")
        if ent["description"]:
            lines.append(f"- description: {ent['description']}")
        if ent.get("pattern_markdown"):
            lines.append("")
            lines.append("```markdown")
            lines.append(ent["pattern_markdown"])
            lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"
