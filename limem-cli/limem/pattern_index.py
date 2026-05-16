"""Pattern Index + Event Metadata + short_id_map 本地 SQLite 缓存。

为什么需要这玩意：
- LiMem 后端 ``/query`` 不返回 metadata，summary 是 LLM 生成的纯文本（不含原始 tag token），
  所以客户端**无法**从 query 结果反推 scope/type。必须本地维护 ``event_id → metadata`` 镜像。
- LiMem 后端 ``/api/entities/{eid}/patterns`` 只能按 entity 列，没有"反查：哪条 prompt 命中哪些 pattern"的端点。
  本地用 FTS5 做 prompt → patterns 反查，延迟 <15ms，确定性 100%。

表结构：
- ``patterns``：每条 pattern 一行；FTS5 索引 ``content``
- ``event_metadata``：每个事件一行；scope/type/project/importance/role/source/ts/raw_metadata（JSON）
- ``short_id_map``：阶段 4 新增，event_id ↔ 短 12 位 id 的全局映射
- 都用 ``tombstone`` 软删除；不直接 DELETE 以便 ``/forget`` 可以撤销
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import PATTERNS_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
  pattern_id   TEXT PRIMARY KEY,
  content      TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  event_id     TEXT,
  pattern_type TEXT,
  scope        TEXT,
  role         TEXT,
  importance   REAL DEFAULT 0.5,
  ts           INTEGER NOT NULL,
  tombstone    INTEGER DEFAULT 0,
  raw_metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_patterns_entity ON patterns(entity_id);
CREATE INDEX IF NOT EXISTS idx_patterns_event  ON patterns(event_id);
CREATE INDEX IF NOT EXISTS idx_patterns_scope  ON patterns(scope);

CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
  content,
  content='patterns',
  content_rowid='rowid',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS patterns_ai AFTER INSERT ON patterns BEGIN
  INSERT INTO patterns_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS patterns_ad AFTER DELETE ON patterns BEGIN
  INSERT INTO patterns_fts(patterns_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS patterns_au AFTER UPDATE ON patterns BEGIN
  INSERT INTO patterns_fts(patterns_fts, rowid, content) VALUES('delete', old.rowid, old.content);
  INSERT INTO patterns_fts(rowid, content) VALUES (new.rowid, new.content);
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

-- 阶段 4：short_id 全局唯一映射
CREATE TABLE IF NOT EXISTS short_id_map (
  short_id   TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL UNIQUE,
  length     INTEGER DEFAULT 12,
  created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_id_event ON short_id_map(event_id);
"""


@dataclass
class PatternHit:
    pattern_id: str
    content: str
    entity_id: str
    event_id: str
    scope: str
    role: str
    importance: float
    bm25_score: float


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


class PatternIndex:
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
            conn.executescript(_SCHEMA)

    # ---------- 写入 ----------

    def upsert_patterns(self, items: Sequence[dict[str, Any]]) -> int:
        now = int(time.time())
        rows = []
        for it in items:
            rows.append(
                (
                    it["pattern_id"],
                    it["content"],
                    it["entity_id"],
                    it.get("event_id") or "",
                    it.get("pattern_type") or "trigger",
                    it.get("scope") or "",
                    it.get("role") or "",
                    float(it.get("importance", 0.5)),
                    int(it.get("ts") or now),
                    json.dumps(it.get("raw_metadata") or {}, ensure_ascii=False),
                )
            )
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO patterns
                  (pattern_id, content, entity_id, event_id, pattern_type, scope, role, importance, ts, raw_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_id) DO UPDATE SET
                  content      = excluded.content,
                  entity_id    = excluded.entity_id,
                  event_id     = excluded.event_id,
                  pattern_type = excluded.pattern_type,
                  scope        = excluded.scope,
                  role         = excluded.role,
                  importance   = excluded.importance,
                  ts           = excluded.ts,
                  raw_metadata = excluded.raw_metadata,
                  tombstone    = 0
                """,
                rows,
            )
        return len(rows)

    def upsert_event_metadata(self, ev: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_metadata
                  (event_id, scope, mem_type, project_id, importance, role, source, ts, summary, raw_metadata)
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

    # ---------- 软删 ----------

    def tombstone_event(self, event_id: str) -> int:
        with self._conn() as conn:
            r1 = conn.execute(
                "UPDATE event_metadata SET tombstone=1 WHERE event_id=?", (event_id,)
            ).rowcount
            r2 = conn.execute(
                "UPDATE patterns SET tombstone=1 WHERE event_id=?", (event_id,)
            ).rowcount
        return r1 + r2

    def tombstone_pattern(self, pattern_id: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE patterns SET tombstone=1 WHERE pattern_id=?", (pattern_id,)
            ).rowcount

    # ---------- 查询 ----------

    def search_patterns(
        self,
        prompt: str,
        *,
        allowed_scopes: Sequence[str],
        limit: int = 20,
    ) -> list[PatternHit]:
        if not prompt.strip():
            return []
        match = _fts5_sanitize(prompt)
        if not match:
            return []
        scope_placeholders = ",".join("?" * len(allowed_scopes))
        sql = f"""
            SELECT p.pattern_id, p.content, p.entity_id, p.event_id,
                   p.scope, p.role, p.importance, fts.rank AS bm25_score
            FROM patterns_fts fts
            JOIN patterns p ON p.rowid = fts.rowid
            WHERE patterns_fts MATCH ?
              AND p.tombstone = 0
              AND p.scope IN ({scope_placeholders})
            ORDER BY fts.rank
            LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (match, *allowed_scopes, limit)).fetchall()
        return [
            PatternHit(
                pattern_id=r["pattern_id"],
                content=r["content"],
                entity_id=r["entity_id"],
                event_id=r["event_id"],
                scope=r["scope"],
                role=r["role"],
                importance=r["importance"] or 0.0,
                bm25_score=r["bm25_score"] or 0.0,
            )
            for r in rows
        ]

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
    ) -> list[EventMetadata]:
        scope_q = ",".join("?" * len(allowed_scopes))
        type_q = ",".join("?" * len(allowed_types))
        sql = f"""
            SELECT * FROM event_metadata
            WHERE tombstone=0
              AND scope IN ({scope_q})
              AND mem_type IN ({type_q})
            ORDER BY importance DESC, ts DESC
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (*allowed_scopes, *allowed_types)).fetchall()
        return [_row_to_meta(r) for r in rows]

    # ---------- 阶段 4：short_id ----------

    def ensure_short_id(self, event_id: str, *, default_length: int = 12) -> str:
        """生成或返回该 event 的 short_id；冲突时逐步扩位（最大 24）。"""
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
        return sha[:default_length]  # 极端情况

    def lookup_event_by_short_id(self, short_id: str) -> str | None:
        short_id = (short_id or "").lstrip("#").strip()
        if not short_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT event_id FROM short_id_map WHERE short_id=?", (short_id,)
            ).fetchone()
        return row["event_id"] if row else None

    # ---------- 阶段 5：iter_all_events ----------

    def iter_all_events(self, *, include_tombstoned: bool = False) -> Iterator[EventMetadata]:
        sql = "SELECT * FROM event_metadata"
        if not include_tombstoned:
            sql += " WHERE tombstone=0"
        sql += " ORDER BY ts DESC"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        for r in rows:
            yield _row_to_meta(r)

    # ---------- 统计 ----------

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            p_total = conn.execute(
                "SELECT count(*) AS c FROM patterns WHERE tombstone=0"
            ).fetchone()["c"]
            p_tomb = conn.execute(
                "SELECT count(*) AS c FROM patterns WHERE tombstone=1"
            ).fetchone()["c"]
            e_total = conn.execute(
                "SELECT count(*) AS c FROM event_metadata WHERE tombstone=0"
            ).fetchone()["c"]
            e_tomb = conn.execute(
                "SELECT count(*) AS c FROM event_metadata WHERE tombstone=1"
            ).fetchone()["c"]
            try:
                short = conn.execute("SELECT count(*) AS c FROM short_id_map").fetchone()["c"]
            except sqlite3.OperationalError:
                short = 0
        return {
            "patterns_active": p_total,
            "patterns_tombstoned": p_tomb,
            "events_active": e_total,
            "events_tombstoned": e_tomb,
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


_FTS_RESERVED = '"():*-'
_CHINESE_WINDOW_SIZE = 4


def _fts5_sanitize(s: str) -> str:
    """构造 FTS5 MATCH 表达式。长中文 token (>4 字) 拆 4 字滑动窗口。"""
    import re

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
