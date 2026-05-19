"""Principal + Event Metadata + short_id 本地 SQLite 镜像（v3）。

v3 重写要点：
- ``principals`` 表是 pattern markdown 的唯一承载体（user / agent / project / team / service）
- 不再维护 mention 级 ``entities`` 与 FTS5 索引；mention 只通过 event_metadata.raw_metadata
  的 ``canonicals`` 字段保留
- 软删除依赖 ``active=0`` 与 ``tombstone=1``；schema 版本不匹配时**直接 unlink 重建**
  （本地缓存视为可重建数据，无迁移）
- event_metadata / short_id_map 字段与 v2 一致，但 raw_metadata 新增 ``principal_ids``

仍然需要本地镜像的原因：
- 后端 ``/patterns/recall`` 只能对单一 entity 召回，hook 内必须先在本地决定 "对哪些
  principal 并发 recall"
- 后端 ``/query`` 不返回 metadata，soft 召回结果必须靠本地 event_metadata 做权威 scope /
  type / principal 过滤
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PATTERNS_DB_PATH

# v1：trigger 短语 FTS（已下线）
# v2：mention 粒度 entities + entities_fts（已下线）
# v3：principals 承载 pattern markdown；mention 仅作为 event tag
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS _schema_meta (
  version    INTEGER PRIMARY KEY,
  applied_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS principals (
  entity_id      TEXT PRIMARY KEY,
  principal_type TEXT NOT NULL,
  slug           TEXT NOT NULL,
  canonical      TEXT NOT NULL,
  aliases        TEXT,
  description    TEXT,
  scope          TEXT NOT NULL,
  tool           TEXT,
  project_id     TEXT,
  has_pattern    INTEGER DEFAULT 0,
  active         INTEGER DEFAULT 1,
  last_seen_ts   INTEGER NOT NULL,
  raw_metadata   TEXT
);
CREATE INDEX IF NOT EXISTS idx_principals_type ON principals(principal_type);
CREATE INDEX IF NOT EXISTS idx_principals_active ON principals(active);
CREATE INDEX IF NOT EXISTS idx_principals_project ON principals(project_id);

CREATE TABLE IF NOT EXISTS event_metadata (
  event_id     TEXT PRIMARY KEY,
  scope        TEXT,
  mem_type     TEXT,
  project_id   TEXT,
  importance   REAL DEFAULT 0.5,
  role         TEXT,
  source       TEXT,
  ts           INTEGER NOT NULL,
  summary      TEXT,
  tombstone    INTEGER DEFAULT 0,
  raw_metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_scope ON event_metadata(scope);
CREATE INDEX IF NOT EXISTS idx_event_type  ON event_metadata(mem_type);

CREATE TABLE IF NOT EXISTS short_id_map (
  short_id   TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL UNIQUE,
  length     INTEGER DEFAULT 12,
  created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_id_event ON short_id_map(event_id);
"""


# ---------- 数据类 ----------


@dataclass
class PrincipalRow:
    entity_id: str
    principal_type: str
    slug: str
    canonical: str
    aliases: list[str]
    description: str
    scope: str
    tool: str
    project_id: str
    has_pattern: bool
    active: bool
    last_seen_ts: int
    raw_metadata: dict[str, Any]


@dataclass
class EventMetadata:
    event_id: str
    scope: str
    mem_type: str
    project_id: str
    importance: float
    role: str
    source: str
    ts: int
    summary: str
    raw_metadata: dict[str, Any]


# v2 兼容别名（仅供旧代码 import 路径平滑过渡；新代码用 PrincipalRow）
EntityHit = PrincipalRow


# ---------- 主类 ----------


class EntityIndex:
    """同步 SQLite 客户端；hook 短生命周期使用足够快。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or PATTERNS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """schema 版本不匹配 → unlink 重建。**不迁移任何旧数据**。"""
        if self.db_path.exists():
            current = 0
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    cur = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema_meta'"
                    ).fetchone()
                    if cur is not None:
                        row = conn.execute("SELECT max(version) AS v FROM _schema_meta").fetchone()
                        current = (row[0] if row else None) or 0
                finally:
                    conn.close()
            except sqlite3.Error:
                current = 0
            if current != SCHEMA_VERSION:
                # 直接删库重建，丢弃旧本地数据（旧 entities / patterns / FTS / metadata）
                try:
                    self.db_path.unlink()
                except FileNotFoundError:
                    pass

        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO _schema_meta(version, applied_ts) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time())),
            )

    # ---------- principals ----------

    def upsert_principal(
        self,
        *,
        entity_id: str,
        principal_type: str,
        slug: str,
        canonical: str,
        aliases: Sequence[str] | None = None,
        description: str = "",
        scope: str = "global",
        tool: str = "",
        project_id: str = "",
        has_pattern: bool | None = None,
        active: bool = True,
        last_seen_ts: int | None = None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> None:
        now = int(last_seen_ts or time.time())
        aliases_json = json.dumps(list(aliases or []), ensure_ascii=False)
        meta_json = json.dumps(raw_metadata or {}, ensure_ascii=False)
        has_pat_value = None if has_pattern is None else (1 if has_pattern else 0)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO principals
                  (entity_id, principal_type, slug, canonical, aliases, description,
                   scope, tool, project_id, has_pattern, active, last_seen_ts, raw_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                  principal_type = excluded.principal_type,
                  slug           = excluded.slug,
                  canonical      = excluded.canonical,
                  aliases        = excluded.aliases,
                  description    = excluded.description,
                  scope          = excluded.scope,
                  tool           = excluded.tool,
                  project_id     = excluded.project_id,
                  has_pattern    = COALESCE(?, principals.has_pattern),
                  active         = excluded.active,
                  last_seen_ts   = excluded.last_seen_ts,
                  raw_metadata   = excluded.raw_metadata
                """,
                (
                    entity_id,
                    principal_type,
                    slug,
                    canonical,
                    aliases_json,
                    description,
                    scope,
                    tool,
                    project_id,
                    has_pat_value,
                    1 if active else 0,
                    now,
                    meta_json,
                    has_pat_value,
                ),
            )

    def list_principals(
        self,
        *,
        active_only: bool = True,
        principal_types: Sequence[str] | None = None,
        project_id: str | None = None,
    ) -> list[PrincipalRow]:
        sql = "SELECT * FROM principals WHERE 1=1"
        params: list[Any] = []
        if active_only:
            sql += " AND active = 1"
        if principal_types:
            placeholders = ",".join("?" * len(principal_types))
            sql += f" AND principal_type IN ({placeholders})"
            params.extend(principal_types)
        if project_id is not None:
            # 项目 principal 精确匹配；其他类型不受 project_id 过滤
            sql += " AND (project_id = ? OR principal_type != 'project')"
            params.append(project_id)
        sql += " ORDER BY last_seen_ts DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_principal(r) for r in rows]

    def lookup_principal(self, entity_id: str) -> PrincipalRow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM principals WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        return _row_to_principal(row) if row else None

    def mark_principal_has_pattern(self, entity_id: str, has_pattern: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE principals SET has_pattern = ? WHERE entity_id = ?",
                (1 if has_pattern else 0, entity_id),
            )

    def activate_principal(self, entity_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE principals SET active = 1, last_seen_ts = ? WHERE entity_id = ?",
                (int(time.time()), entity_id),
            )

    def deactivate_principal(self, entity_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE principals SET active = 0 WHERE entity_id = ?",
                (entity_id,),
            )

    # v2 兼容别名（mcp_server / hooks 旧调用路径）
    def mark_has_pattern(self, entity_id: str, has_pattern: bool) -> None:
        self.mark_principal_has_pattern(entity_id, has_pattern)

    def lookup_entity(self, entity_id: str) -> PrincipalRow | None:
        return self.lookup_principal(entity_id)

    # ---------- Event metadata ----------

    def upsert_event_metadata(self, ev: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_metadata
                  (event_id, scope, mem_type, project_id, importance, role, source,
                   ts, summary, raw_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                  scope        = excluded.scope,
                  mem_type     = excluded.mem_type,
                  project_id   = excluded.project_id,
                  importance   = excluded.importance,
                  role         = excluded.role,
                  source       = excluded.source,
                  ts           = excluded.ts,
                  summary      = excluded.summary,
                  raw_metadata = excluded.raw_metadata,
                  tombstone    = 0
                """,
                (
                    ev["event_id"],
                    ev.get("scope") or "",
                    ev.get("mem_type") or "",
                    ev.get("project_id") or "",
                    float(ev.get("importance", 0.5)),
                    ev.get("role") or "",
                    ev.get("source") or "",
                    int(ev.get("ts") or int(time.time())),
                    ev.get("summary") or "",
                    json.dumps(ev.get("raw_metadata") or {}, ensure_ascii=False),
                ),
            )

    def tombstone_event(self, event_id: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE event_metadata SET tombstone=1 WHERE event_id=?", (event_id,)
            ).rowcount

    def lookup_event(self, event_id: str) -> EventMetadata | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM event_metadata WHERE event_id=? AND tombstone=0",
                (event_id,),
            ).fetchone()
        if not row:
            return None
        return _row_to_meta(row)

    def filter_query_results(
        self,
        results: Iterable[Any],
        *,
        allowed_scopes: set[str],
        excluded_types: set[str] | None = None,
        allowed_types: set[str] | None = None,
        allowed_principals: set[str] | None = None,
    ) -> list[tuple[Any, EventMetadata]]:
        """对后端 query() 结果做权威 scope/type 过滤。

        ``allowed_principals`` 不为空时执行 v3 降权语义：raw_metadata.principal_ids 与
        allowed 无交集的项 ``score *= 0.5``，但**不丢弃**（用户决策）。
        """
        kept: list[tuple[Any, EventMetadata]] = []
        for r in results:
            event_id = getattr(r, "event_id", None) or (
                r.get("event_id") if isinstance(r, dict) else None
            )
            if not event_id:
                continue
            meta = self.lookup_event(event_id)
            if meta is None:
                continue
            if allowed_scopes and meta.scope not in allowed_scopes:
                continue
            if excluded_types and meta.mem_type in excluded_types:
                continue
            if allowed_types and meta.mem_type not in allowed_types:
                continue
            if allowed_principals:
                pids = set((meta.raw_metadata or {}).get("principal_ids") or [])
                if pids and not (pids & allowed_principals):
                    try:
                        r.score = float(getattr(r, "score", 0.0) or 0.0) * 0.5
                    except Exception:
                        pass
            kept.append((r, meta))
        return kept

    def list_hard_recall(
        self,
        *,
        allowed_scopes: Sequence[str],
        allowed_types: Sequence[str],
        min_importance: float = 0.0,
    ) -> list[EventMetadata]:
        if not allowed_scopes or not allowed_types:
            return []
        scope_q = ",".join("?" * len(allowed_scopes))
        type_q = ",".join("?" * len(allowed_types))
        sql = f"""
            SELECT * FROM event_metadata
            WHERE tombstone=0
              AND scope IN ({scope_q})
              AND mem_type IN ({type_q})
              AND importance >= ?
            ORDER BY importance DESC, ts DESC
        """
        with self._conn() as conn:
            rows = conn.execute(
                sql, (*allowed_scopes, *allowed_types, float(min_importance))
            ).fetchall()
        return [_row_to_meta(r) for r in rows]

    # ---------- short_id ----------

    def ensure_short_id(self, event_id: str, *, default_length: int = 12) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT short_id FROM short_id_map WHERE event_id=?", (event_id,)
            ).fetchone()
            if row:
                return row["short_id"]
            sha = hashlib.sha1(event_id.encode()).hexdigest()
            for length in range(default_length, min(25, len(sha))):
                candidate = sha[:length]
                exists = conn.execute(
                    "SELECT 1 FROM short_id_map WHERE short_id=?", (candidate,)
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO short_id_map (short_id, event_id, length, created_ts) "
                        "VALUES (?, ?, ?, ?)",
                        (candidate, event_id, length, int(time.time())),
                    )
                    return candidate
        return sha[:default_length]

    def lookup_event_by_short_id(self, short_id: str) -> str | None:
        s = (short_id or "").lstrip("#").strip()
        if not s:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT event_id FROM short_id_map WHERE short_id=?", (s,)
            ).fetchone()
        return row["event_id"] if row else None

    # ---------- 杂项 ----------

    def iter_all_events(self, *, include_tombstoned: bool = False) -> Iterator[EventMetadata]:
        sql = "SELECT * FROM event_metadata"
        if not include_tombstoned:
            sql += " WHERE tombstone=0"
        sql += " ORDER BY ts DESC"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        for r in rows:
            yield _row_to_meta(r)

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            p_active = conn.execute(
                "SELECT count(*) AS c FROM principals WHERE active=1"
            ).fetchone()["c"]
            p_with_pat = conn.execute(
                "SELECT count(*) AS c FROM principals WHERE active=1 AND has_pattern=1"
            ).fetchone()["c"]
            ev_total = conn.execute(
                "SELECT count(*) AS c FROM event_metadata WHERE tombstone=0"
            ).fetchone()["c"]
            ev_tomb = conn.execute(
                "SELECT count(*) AS c FROM event_metadata WHERE tombstone=1"
            ).fetchone()["c"]
            try:
                short = conn.execute("SELECT count(*) AS c FROM short_id_map").fetchone()["c"]
            except sqlite3.OperationalError:
                short = 0
        return {
            "principals_active": p_active,
            "principals_with_pattern": p_with_pat,
            # v2 兼容字段名（外部脚本 / dashboard 可能仍依赖）
            "entities_active": p_active,
            "entities_with_pattern": p_with_pat,
            "events_active": ev_total,
            "events_tombstoned": ev_tomb,
            "short_ids": short,
        }


# ---------- row → dataclass ----------


def _row_to_principal(row: sqlite3.Row | None) -> PrincipalRow | None:
    if row is None:
        return None
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except json.JSONDecodeError:
        aliases = []
    try:
        raw = json.loads(row["raw_metadata"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    return PrincipalRow(
        entity_id=row["entity_id"],
        principal_type=row["principal_type"] or "",
        slug=row["slug"] or "",
        canonical=row["canonical"] or "",
        aliases=aliases if isinstance(aliases, list) else [],
        description=row["description"] or "",
        scope=row["scope"] or "",
        tool=row["tool"] or "",
        project_id=row["project_id"] or "",
        has_pattern=bool(row["has_pattern"]),
        active=bool(row["active"]),
        last_seen_ts=int(row["last_seen_ts"] or 0),
        raw_metadata=raw if isinstance(raw, dict) else {},
    )


def _row_to_meta(row: sqlite3.Row) -> EventMetadata:
    try:
        raw = json.loads(row["raw_metadata"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    return EventMetadata(
        event_id=row["event_id"],
        scope=row["scope"] or "",
        mem_type=row["mem_type"] or "",
        project_id=row["project_id"] or "",
        importance=row["importance"] or 0.0,
        role=row["role"] or "",
        source=row["source"] or "",
        ts=row["ts"] or 0,
        summary=row["summary"] or "",
        raw_metadata=raw if isinstance(raw, dict) else {},
    )
