"""schema 不匹配时 EntityIndex 应该 unlink 重建（v3 行为）。"""

from __future__ import annotations

import sqlite3

from limem.entity_index import SCHEMA_VERSION, EntityIndex


def _create_legacy_v2_db(path) -> None:
    """模拟 v2 schema（entities + entities_fts + event_metadata）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE _schema_meta (
              version INTEGER PRIMARY KEY,
              applied_ts INTEGER NOT NULL
            );
            INSERT INTO _schema_meta(version, applied_ts) VALUES (2, 0);

            CREATE TABLE entities (
              entity_id TEXT PRIMARY KEY,
              canonical TEXT,
              aliases TEXT,
              description TEXT,
              entity_type TEXT,
              scope TEXT,
              role TEXT,
              importance REAL,
              last_seen_ts INTEGER NOT NULL,
              tombstone INTEGER,
              has_pattern INTEGER,
              raw_metadata TEXT
            );

            CREATE TABLE event_metadata (
              event_id TEXT PRIMARY KEY,
              scope TEXT,
              mem_type TEXT,
              project_id TEXT,
              importance REAL,
              role TEXT,
              source TEXT,
              ts INTEGER NOT NULL,
              summary TEXT,
              tombstone INTEGER,
              raw_metadata TEXT
            );

            CREATE TABLE short_id_map (
              short_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              length INTEGER,
              created_ts INTEGER NOT NULL
            );
            """
        )
        # 写入一条旧 entity 数据，验证重建后会被丢弃
        conn.execute(
            "INSERT INTO entities (entity_id, canonical, last_seen_ts, importance, "
            "has_pattern, tombstone, aliases, description, entity_type, scope, role, raw_metadata) "
            "VALUES ('legacy_canonical_1', 'npm run dev', 0, 0.9, 1, 0, '[]', '', '', "
            "'project:foo/bar', 'forbidden', '{}')"
        )
        conn.commit()
    finally:
        conn.close()


def test_v2_db_is_rebuilt_to_v3(tmp_path) -> None:
    db_path = tmp_path / "patterns.sqlite"
    _create_legacy_v2_db(db_path)

    # 健全检查：旧库里有数据
    pre = sqlite3.connect(str(db_path))
    try:
        rows = pre.execute("SELECT version FROM _schema_meta").fetchall()
        assert rows == [(2,)]
        legacy_rows = pre.execute("SELECT count(*) FROM entities").fetchone()[0]
        assert legacy_rows == 1
    finally:
        pre.close()

    # 实例化 EntityIndex：应触发 unlink + 重建
    idx = EntityIndex(db_path=db_path)

    # 重建后 schema_meta 升级
    s = idx.stats()
    assert s["principals_active"] == 0
    assert s["events_active"] == 0

    # 旧 entities 表已不存在；新 principals 表存在
    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("SELECT max(version) FROM _schema_meta").fetchone()[0]
        assert version == SCHEMA_VERSION == 3
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "principals" in tables
        assert "event_metadata" in tables
        assert "short_id_map" in tables
        # 旧 entities 表不应再存在
        assert "entities" not in tables
    finally:
        conn.close()


def test_fresh_db_creates_v3_schema(tmp_path) -> None:
    db_path = tmp_path / "patterns.sqlite"
    assert not db_path.exists()
    idx = EntityIndex(db_path=db_path)
    assert db_path.exists()
    assert idx.stats()["principals_active"] == 0


def test_v1_or_unknown_version_also_rebuilds(tmp_path) -> None:
    """version=1（旧 trigger 短语阶段）应该和 v2 一样直接 unlink。"""
    db_path = tmp_path / "patterns.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE _schema_meta (version INTEGER PRIMARY KEY, applied_ts INTEGER NOT NULL);
            INSERT INTO _schema_meta(version, applied_ts) VALUES (1, 0);
            CREATE TABLE patterns (canonical TEXT);
            INSERT INTO patterns(canonical) VALUES ('legacy_trigger');
            """
        )
        conn.commit()
    finally:
        conn.close()

    EntityIndex(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "patterns" not in tables
        assert "principals" in tables
        version = conn.execute("SELECT max(version) FROM _schema_meta").fetchone()[0]
        assert version == 3
    finally:
        conn.close()


def test_principal_crud_roundtrip(tmp_path) -> None:
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")
    idx.upsert_principal(
        entity_id="principal_project_deadbeef",
        principal_type="project",
        slug="github.com/foo/bar",
        canonical="project:bar",
        aliases=["bar", "本项目"],
        description="测试项目",
        scope="project:github.com/foo/bar",
        project_id="github.com/foo/bar",
        active=True,
    )
    rows = idx.list_principals()
    assert len(rows) == 1
    p = rows[0]
    assert p.principal_type == "project"
    assert p.slug == "github.com/foo/bar"
    assert "bar" in p.aliases
    assert p.has_pattern is False

    idx.mark_principal_has_pattern(p.entity_id, True)
    assert idx.lookup_principal(p.entity_id).has_pattern is True

    idx.deactivate_principal(p.entity_id)
    assert idx.list_principals(active_only=True) == []
    assert len(idx.list_principals(active_only=False)) == 1

    idx.activate_principal(p.entity_id)
    assert len(idx.list_principals(active_only=True)) == 1
