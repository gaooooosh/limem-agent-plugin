"""``limem export`` — 本地全量导出（JSON / Markdown）。

约束：仅读 SQLite + 对每条 event 调一次 ``client.list_entity_patterns`` 补 triggers；
**不**调 ``query`` 端点（避免污染缓存与 BM25 stats）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .client import LimemClient, LimemError
from .config import Credentials
from .pattern_index import EventMetadata, PatternIndex


def _collect_events(*, include_tombstoned: bool) -> list[dict[str, Any]]:
    pidx = PatternIndex()
    out: list[dict[str, Any]] = []
    for meta in pidx.iter_all_events(include_tombstoned=include_tombstoned):
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
                "triggers": [],
            }
        )
    return out


def _fill_triggers(items: list[dict[str, Any]], creds: Credentials) -> None:
    if not creds.api_key or not creds.db_id:
        return
    client = LimemClient(creds=creds)
    pidx = PatternIndex()
    seen_entities: set[str] = set()
    for it in items:
        with pidx._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT entity_id FROM patterns WHERE event_id=? AND tombstone=0",
                (it["event_id"],),
            ).fetchall()
        for r in rows:
            eid = r["entity_id"]
            if not eid or eid in seen_entities:
                continue
            seen_entities.add(eid)
            try:
                patterns = client.list_entity_patterns(eid)
                it["triggers"].extend(p.content for p in patterns)
            except LimemError:
                pass


def export(
    *,
    fmt: str = "json",
    output: Path | None = None,
    include_tombstoned: bool = False,
    fill_triggers: bool = True,
) -> Path:
    """fmt: ``json`` | ``markdown``；返回写入的文件路径（chmod 600）。"""
    items = _collect_events(include_tombstoned=include_tombstoned)
    if fill_triggers:
        try:
            _fill_triggers(items, Credentials.load())
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
            "schema_version": 1,
            "events": items,
        }
        output.write_text(json.dumps(body, ensure_ascii=False, indent=2))
    elif fmt == "markdown":
        output.write_text(_to_markdown(items))
    else:
        raise ValueError(f"unknown format: {fmt}")

    try:
        os.chmod(output, 0o600)
    except OSError:
        pass
    return output


def _to_markdown(items: list[dict[str, Any]]) -> str:
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_scope.setdefault(it["scope"], []).append(it)
    lines: list[str] = ["# LiMem export", ""]
    for scope, scoped in sorted(by_scope.items()):
        lines.append(f"## {scope}")
        by_type: dict[str, list[dict[str, Any]]] = {}
        for it in scoped:
            by_type.setdefault(it["mem_type"], []).append(it)
        for mt, mt_items in sorted(by_type.items()):
            lines.append(f"### {mt}")
            for it in mt_items:
                short = it["event_id"][:12]
                text = it.get("original_text") or it.get("summary") or ""
                lines.append(f"- [#{short}] {text} (importance={it['importance']:.2f})")
                trig = it.get("triggers") or []
                if trig:
                    lines.append(f"  - triggers: {', '.join(trig[:8])}")
            lines.append("")
    return "\n".join(lines) + "\n"
