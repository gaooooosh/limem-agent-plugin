"""Entity Index + Event Metadata + short_id 本地 SQLite 缓存。

v2 重写：
- 旧 ``patterns`` / ``patterns_fts`` 表（trigger 短语 trigram 索引）首次启动自动 DROP。
- 新 ``entities`` / ``entities_fts`` 表：以**注册实体**为索引主体，FTS 字段为
  canonical + aliases + description。命中后客户端再去后端拉对应 markdown 切片。
- ``event_metadata`` / ``short_id_map`` 保留（与 pattern 无关，仍是后端 query summary
  反查 scope/type 的权威镜像）。

为什么仍然需要本地索引：
- 后端 ``GET /api/entities/{id}/patterns/recall`` 只能针对**单个 entity**召回 markdown。
  hook 在 UserPromptSubmit 时间窗内必须先快速决定 "哪些 entity 与当前 prompt 相关"，
  这一步靠本地 FTS5 trigram（中英文混合，<15 ms）。
- 后端 ``POST /query`` 不返回 metadata，summary 是 LLM 生成的纯文本（不含原始 tag token）；
  仍需本地 event_metadata 镜像反查 scope/type。

软删除用 ``tombstone`` 标记，便于 ``/limem.forget`` 撤销。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PATTERNS_DB_PATH

# schema_version 用于驱动 v1 → v2 的本地表迁移。
# v1：仅有 patterns/patterns_fts/event_metadata（trigger 短语索引）
# v2：删除 patterns/patterns_fts，新增 entities/entities_fts；event_metadata + short_id_map 保留
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS _schema_meta (
  version    INTEGER PRIMARY KEY,
  applied_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  entity_id     TEXT PRIMARY KEY,
  canonical     TEXT NOT NULL,
  aliases       TEXT,                -- JSON list[str]
  description   TEXT,
  entity_type   TEXT,
  scope         TEXT NOT NULL,
  role          TEXT,
  importance    REAL DEFAULT 0.5,
  last_seen_ts  INTEGER NOT NULL,
  tombstone     INTEGER DEFAULT 0,
  has_pattern   INTEGER DEFAULT 0,   -- 后端是否已挂 markdown
  raw_metadata  TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_scope ON entities(scope);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
  fts_text,
  content='entities',
  content_rowid='rowid',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, fts_text)
  VALUES (new.rowid,
    coalesce(new.canonical, '') || ' ' ||
    coalesce(new.aliases, '') || ' ' ||
    coalesce(new.description, ''));
END;
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, fts_text)
  VALUES('delete', old.rowid,
    coalesce(old.canonical, '') || ' ' ||
    coalesce(old.aliases, '') || ' ' ||
    coalesce(old.description, ''));
END;
CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, fts_text)
  VALUES('delete', old.rowid,
    coalesce(old.canonical, '') || ' ' ||
    coalesce(old.aliases, '') || ' ' ||
    coalesce(old.description, ''));
  INSERT INTO entities_fts(rowid, fts_text)
  VALUES (new.rowid,
    coalesce(new.canonical, '') || ' ' ||
    coalesce(new.aliases, '') || ' ' ||
    coalesce(new.description, ''));
END;

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


@dataclass
class EntityHit:
    entity_id: str
    canonical: str
    aliases: list[str]
    description: str
    scope: str
    role: str
    importance: float
    has_pattern: bool
    bm25_score: float

    def composite_score(self) -> float:
        """FTS bm25 是负值，越接近 0 越好；importance ∈ [0,1] 越大越好。"""
        return float(self.importance or 0.0) - float(self.bm25_score or 0.0) * 0.1


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
        with self._conn() as conn:
            # 1) 创建 schema_meta（若不存在）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _schema_meta "
                "(version INTEGER PRIMARY KEY, applied_ts INTEGER NOT NULL)"
            )
            row = conn.execute(
                "SELECT max(version) AS v FROM _schema_meta"
            ).fetchone()
            current = (row["v"] if row else None) or 0

            # 2) v1 → v2 迁移：DROP 旧 trigger 短语表
            if current < SCHEMA_VERSION:
                conn.execute("DROP TABLE IF EXISTS patterns_fts")
                conn.execute("DROP TRIGGER IF EXISTS patterns_ai")
                conn.execute("DROP TRIGGER IF EXISTS patterns_ad")
                conn.execute("DROP TRIGGER IF EXISTS patterns_au")
                conn.execute("DROP TABLE IF EXISTS patterns")

            # 3) 重建/创建新 schema（幂等）
            conn.executescript(_SCHEMA)

            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO _schema_meta(version, applied_ts) VALUES (?, ?)",
                    (SCHEMA_VERSION, int(time.time())),
                )

    # ---------- Entity 写入 ----------

    def upsert_entity(
        self,
        *,
        entity_id: str,
        canonical: str,
        aliases: Sequence[str] | None = None,
        description: str = "",
        entity_type: str = "",
        scope: str = "",
        role: str = "",
        importance: float = 0.5,
        has_pattern: bool = False,
        raw_metadata: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> None:
        now = int(ts or time.time())
        aliases_json = json.dumps(list(aliases or []), ensure_ascii=False)
        meta_json = json.dumps(raw_metadata or {}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO entities
                  (entity_id, canonical, aliases, description, entity_type,
                   scope, role, importance, last_seen_ts, has_pattern, raw_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                  canonical    = excluded.canonical,
                  aliases      = excluded.aliases,
                  description  = excluded.description,
                  entity_type  = excluded.entity_type,
                  scope        = excluded.scope,
                  role         = excluded.role,
                  importance   = max(entities.importance, excluded.importance),
                  last_seen_ts = excluded.last_seen_ts,
                  has_pattern  = excluded.has_pattern,
                  raw_metadata = excluded.raw_metadata,
                  tombstone    = 0
                """,
                (
                    entity_id,
                    canonical,
                    aliases_json,
                    description,
                    entity_type,
                    scope,
                    role,
                    float(importance),
                    now,
                    1 if has_pattern else 0,
                    meta_json,
                ),
            )

    def mark_has_pattern(self, entity_id: str, has_pattern: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE entities SET has_pattern=? WHERE entity_id=?",
                (1 if has_pattern else 0, entity_id),
            )

    def tombstone_entity(self, entity_id: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE entities SET tombstone=1 WHERE entity_id=?", (entity_id,)
            ).rowcount

    # ---------- Entity 查询 ----------

    def search_entities(
        self,
        prompt: str,
        *,
        allowed_scopes: Sequence[str],
        limit: int = 5,
        require_pattern: bool = False,
    ) -> list[EntityHit]:
        """根据 prompt 在 entity FTS5 索引上反查候选实体。"""
        if not prompt.strip() or not allowed_scopes:
            return []
        match = _fts5_sanitize(prompt)
        if not match:
            return []
        scope_placeholders = ",".join("?" * len(allowed_scopes))
        pattern_filter = " AND e.has_pattern = 1" if require_pattern else ""
        sql = f"""
            SELECT e.entity_id, e.canonical, e.aliases, e.description, e.scope,
                   e.role, e.importance, e.has_pattern, fts.rank AS bm25_score
            FROM entities_fts fts
            JOIN entities e ON e.rowid = fts.rowid
            WHERE entities_fts MATCH ?
              AND e.tombstone = 0
              AND e.scope IN ({scope_placeholders})
              {pattern_filter}
            ORDER BY fts.rank
            LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (match, *allowed_scopes, limit * 2)).fetchall()
        hits: list[EntityHit] = []
        for r in rows:
            try:
                aliases = json.loads(r["aliases"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            hits.append(
                EntityHit(
                    entity_id=r["entity_id"],
                    canonical=r["canonical"] or "",
                    aliases=aliases if isinstance(aliases, list) else [],
                    description=r["description"] or "",
                    scope=r["scope"] or "",
                    role=r["role"] or "",
                    importance=float(r["importance"] or 0.0),
                    has_pattern=bool(r["has_pattern"]),
                    bm25_score=float(r["bm25_score"] or 0.0),
                )
            )
        hits.sort(key=lambda h: h.composite_score(), reverse=True)
        return hits[:limit]

    def lookup_entity(self, entity_id: str) -> EntityHit | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entity_id, canonical, aliases, description, scope, role, "
                "       importance, has_pattern, 0 AS bm25_score "
                "FROM entities WHERE entity_id=? AND tombstone=0",
                (entity_id,),
            ).fetchone()
        if not row:
            return None
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        return EntityHit(
            entity_id=row["entity_id"],
            canonical=row["canonical"] or "",
            aliases=aliases if isinstance(aliases, list) else [],
            description=row["description"] or "",
            scope=row["scope"] or "",
            role=row["role"] or "",
            importance=float(row["importance"] or 0.0),
            has_pattern=bool(row["has_pattern"]),
            bm25_score=0.0,
        )

    # ---------- Event metadata（不变接口） ----------

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
    ) -> list[tuple[Any, EventMetadata]]:
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

    # ---------- short_id（不变） ----------

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
            e_total = conn.execute(
                "SELECT count(*) AS c FROM entities WHERE tombstone=0"
            ).fetchone()["c"]
            e_with_pattern = conn.execute(
                "SELECT count(*) AS c FROM entities WHERE tombstone=0 AND has_pattern=1"
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
            "entities_active": e_total,
            "entities_with_pattern": e_with_pattern,
            "events_active": ev_total,
            "events_tombstoned": ev_tomb,
            "short_ids": short,
        }


def _row_to_meta(row: sqlite3.Row) -> EventMetadata:
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
        raw_metadata=json.loads(row["raw_metadata"] or "{}"),
    )


# ---------- FTS5 表达式构造（沿用旧版的中文长 token 滑窗） ----------

_FTS_RESERVED = '"():*-'
_CHINESE_WINDOW_SIZE = 4


def _fts5_sanitize(s: str) -> str:
    """构造 FTS5 MATCH 表达式。长中文 token (>4 字) 拆 4 字滑动窗口。"""
    cleaned = "".join(" " if c in _FTS_RESERVED else c for c in s)
    tokens = re.findall(r"[一-鿿\w]+", cleaned, re.UNICODE)
    if not tokens:
        return ""
    seen: set[str] = set()
    quoted: list[str] = []
    for t in tokens:
        t = t.lower()
        if len(t) < 2:
            continue
        if _has_chinese(t) and len(t) > _CHINESE_WINDOW_SIZE:
            for i in range(len(t) - _CHINESE_WINDOW_SIZE + 1):
                sub = t[i : i + _CHINESE_WINDOW_SIZE]
                if sub in seen:
                    continue
                seen.add(sub)
                quoted.append(f'"{sub}"')
                if len(quoted) >= 32:
                    break
        else:
            if t in seen:
                continue
            seen.add(t)
            quoted.append(f'"{t}"')
        if len(quoted) >= 32:
            break
    return " OR ".join(quoted)


def _has_chinese(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)
