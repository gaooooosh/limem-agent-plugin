"""LiMem MCP stdio server。

v2 工具集（变化集中在 search / write / pattern_* / fix）：
- ``limem_search``  ：entity FTS5 候选 → 并发后端 markdown 切片 + BM25 软召回，三段聚合
- ``limem_write``   ：写一条 event + 注册（晋升）entity；**不再支持 trigger 短语数组**
- ``limem_forget``  ：归档 event
- ``limem_fix``     ：修改 event 文本（不动 entity markdown）
- ``limem_list``    ：列出本项目 + global 的强规则
- ``limem_pause``   / ``limem_resume`` / ``limem_mute``  ：保留
- ``limem_ping``    / ``limem_stats`` ：保留（stats 输出新 entity 维度）
- ``limem_pattern_get`` / ``limem_pattern_put`` / ``limem_pattern_delete``  ：v2 新增
   entity markdown 档案的 CRUD（唯一面向 LLM 的入口；用户侧通过 /limem.pattern skill）
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import daemon_client, session_mute
from .client import LimemClient, LimemError, PatternRecallResult
from .config import Credentials, RuntimeConfig
from .entity_index import EntityIndex
from .memory_writer import (
    EntitySpec,
    delete_pattern as do_pattern_delete,
    fix as do_fix,
    forget as do_forget,
    get_pattern as do_pattern_get,
    remember as do_remember,
    update_pattern as do_pattern_put,
)
from .scope import detect_project_id


server: Server = Server("limem")


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name="limem_search",
            description=(
                "Search LiMem long-term memory for the current user/project. "
                "Returns matched memories (rules / events) AND any relevant entity "
                "markdown sections (`pattern` source). Use when you need to recall "
                "what the user has previously asked you to remember."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "include_types": {"type": "array", "items": {"type": "string"}},
                    "include_patterns": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="limem_write",
            description=(
                "Persist a long-term memory event. Use when user says 'remember X' / "
                "'don't do Y' / 'always Z'. To edit an entity's markdown profile use "
                "limem_pattern_put instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["project", "global", "session"],
                        "default": "project",
                    },
                    "mem_type": {
                        "type": "string",
                        "enum": ["rule", "feedback", "preference", "note", "fact", "decision"],
                        "default": "rule",
                    },
                    "importance": {"type": "number", "default": 0.9, "minimum": 0, "maximum": 1},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "canonical": {"type": "string"},
                                "role": {
                                    "type": "string",
                                    "enum": ["forbidden", "preferred", "neutral", "subject"],
                                },
                                "aliases": {"type": "array", "items": {"type": "string"}},
                                "description": {"type": "string"},
                                "entity_type": {"type": "string"},
                            },
                            "required": ["canonical"],
                        },
                    },
                    "session_id": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="limem_forget",
            description="Archive a memory event by ID or short_id (#xxxx).",
            inputSchema={
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
        ),
        Tool(
            name="limem_list",
            description="List all active rule/feedback/preference memories for current project + global.",
            inputSchema={
                "type": "object",
                "properties": {
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["rule", "feedback", "preference"],
                    },
                    "include_global": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="limem_ping",
            description="Probe connectivity to LiMem backend.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="limem_stats",
            description="Show local SQLite cache statistics (entities/events/short_ids).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="limem_pause",
            description=(
                "Suspend all LiMem recall + capture. Use when user says 'pause limem' / 'mute memory'. "
                "duration_seconds 0 = until session end (no auto-resume)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "duration_seconds": {"type": "integer", "default": 3600},
                    "scope": {"type": "string", "enum": ["project", "global"], "default": "project"},
                    "session_id": {"type": "string"},
                },
            },
        ),
        Tool(
            name="limem_resume",
            description="Clear pause state immediately.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="limem_fix",
            description=(
                "Update an existing memory event's text in-place via short_id (#xxxx) "
                "or full event_id. Does NOT create a new event. For editing an entity's "
                "markdown profile, use limem_pattern_put instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "short_id": {"type": "string", "description": "可填 #xxxx 形式或完整 event_id"},
                    "new_text": {"type": "string"},
                },
                "required": ["short_id", "new_text"],
            },
        ),
        Tool(
            name="limem_mute",
            description=(
                "Mute a specific memory for the current session only (until SessionEnd/Stop). "
                "Does not touch backend."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "short_id": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["short_id", "session_id"],
            },
        ),
        # v2 新增：entity markdown 档案 CRUD
        Tool(
            name="limem_pattern_get",
            description=(
                "Fetch the entire markdown profile attached to a registered entity. "
                "Use to read what is currently stored before editing."
            ),
            inputSchema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        ),
        Tool(
            name="limem_pattern_put",
            description=(
                "Replace an entity's markdown profile with new content (whole-document upsert). "
                "Caller is responsible for merging if partial edits are intended."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "Full markdown content; non-empty. H2 sections will be used for recall scoring.",
                    },
                },
                "required": ["entity_id", "content"],
            },
        ),
        Tool(
            name="limem_pattern_delete",
            description="Hard-delete an entity's markdown profile. The entity itself is NOT removed.",
            inputSchema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        ),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "limem_search":
            text = _t_search(**arguments)
        elif name == "limem_write":
            text = _t_write(**arguments)
        elif name == "limem_forget":
            text = _t_forget(**arguments)
        elif name == "limem_list":
            text = _t_list(**arguments)
        elif name == "limem_ping":
            text = _t_ping()
        elif name == "limem_stats":
            text = _t_stats()
        elif name == "limem_pause":
            text = _t_pause(**arguments)
        elif name == "limem_resume":
            text = _t_resume()
        elif name == "limem_fix":
            text = _t_fix(**arguments)
        elif name == "limem_mute":
            text = _t_mute(**arguments)
        elif name == "limem_pattern_get":
            text = _t_pattern_get(**arguments)
        elif name == "limem_pattern_put":
            text = _t_pattern_put(**arguments)
        elif name == "limem_pattern_delete":
            text = _t_pattern_delete(**arguments)
        else:
            text = json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    except (LimemError, ValueError) as e:
        text = json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        text = json.dumps({"error": "internal", "detail": str(e)}, ensure_ascii=False)
    return [TextContent(type="text", text=text)]


def _allowed_scopes(project_id: str) -> list[str]:
    return ["global"] + ([f"project:{project_id}"] if project_id else [])


def _resolve_event_id(input_id: str) -> str:
    s = (input_id or "").strip()
    if s.startswith("#"):
        s = s[1:]
    idx = EntityIndex()
    looked = idx.lookup_event_by_short_id(s)
    return looked or input_id


def _t_search(
    query: str,
    *,
    top_k: int = 5,
    include_types: list[str] | None = None,
    include_patterns: bool = True,
) -> str:
    creds = Credentials.load()
    runtime = RuntimeConfig.load()
    project_id = detect_project_id()
    scopes = _allowed_scopes(project_id)
    idx = EntityIndex()

    out_events: list[dict[str, Any]] = []
    out_patterns: list[dict[str, Any]] = []

    # 1) entity FTS → 后端 markdown 切片
    if include_patterns and creds.api_key and creds.db_id:
        entity_hits = idx.search_entities(
            query,
            allowed_scopes=scopes,
            limit=min(top_k, runtime.pattern_top_entities),
            require_pattern=True,
        )
        client = LimemClient(
            creds=creds, timeout=runtime.patterns_recall_timeout_ms / 1000.0
        )
        for eh in entity_hits:
            try:
                res: PatternRecallResult = client.patterns_recall(
                    eh.entity_id,
                    query,
                    mode="auto",
                    top_k_sections=runtime.patterns_recall_top_k_sections,
                )
            except LimemError:
                continue
            except Exception:
                continue
            if not res.has_content():
                continue
            out_patterns.append(
                {
                    "entity_id": eh.entity_id,
                    "canonical": eh.canonical,
                    "scope": eh.scope,
                    "mode": res.mode,
                    "matched_sections": [
                        {"heading": s.heading, "score": s.score} for s in res.matched_sections
                    ],
                    "content": res.content,
                    "source": "pattern",
                }
            )

    # 2) BM25 软召回
    soft_results = []
    if creds.api_key and creds.db_id:
        client = LimemClient(creds=creds)
        try:
            soft_results = client.query(query, top_k=top_k * 2)
        except LimemError:
            soft_results = []

    excluded = (
        {"rule", "feedback", "preference"} - set(include_types or [])
        if include_types
        else set()
    )
    soft_filtered = idx.filter_query_results(
        soft_results,
        allowed_scopes=set(scopes),
        allowed_types=set(include_types) if include_types else None,
        excluded_types=excluded if not include_types else None,
    )

    seen: set[str] = set()
    for qr, meta in soft_filtered:
        if meta.event_id in seen:
            continue
        seen.add(meta.event_id)
        out_events.append(_serialize_event(meta, source="bm25", score=qr.score))
        if len(out_events) >= top_k:
            break

    return json.dumps(
        {
            "events": out_events,
            "patterns": out_patterns[:top_k],
            "project_id": project_id,
        },
        ensure_ascii=False,
    )


def _serialize_event(meta, *, source: str, score: float | None = None) -> dict[str, Any]:
    raw = meta.raw_metadata or {}
    idx = EntityIndex()
    try:
        short = idx.ensure_short_id(meta.event_id)
    except Exception:
        short = meta.event_id[:12]
    return {
        "event_id": meta.event_id,
        "short_id": short,
        "type": meta.mem_type,
        "scope": meta.scope,
        "role": meta.role,
        "importance": meta.importance,
        "source": source,
        "bm25_score": score,
        "text": raw.get("original_text") or meta.summary,
        "canonicals": raw.get("canonicals", []),
    }


def _t_write(
    text: str,
    *,
    scope: str = "project",
    mem_type: str = "rule",
    importance: float = 0.9,
    entities: list[dict[str, Any]] | None = None,
    session_id: str = "",
    detail: str = "",
) -> str:
    project_id = detect_project_id()
    if scope == "project":
        effective_scope = f"project:{project_id}"
    elif scope == "session":
        effective_scope = f"session:{session_id or 'adhoc'}"
    else:
        effective_scope = "global"

    ents: list[EntitySpec] = []
    for raw in entities or []:
        ents.append(
            EntitySpec(
                canonical=raw["canonical"],
                role=raw.get("role", "neutral"),
                aliases=list(raw.get("aliases") or []),
                description=raw.get("description") or "",
                entity_type=raw.get("entity_type") or "",
            )
        )

    res = do_remember(
        text=text,
        scope=effective_scope,
        mem_type=mem_type,
        importance=importance,
        project_id=project_id,
        entities=ents or None,
        source="mcp:limem_write",
        session_id=session_id,
        detail=detail,
    )
    return json.dumps(
        {
            "event_id": res.event_id,
            "scope": res.scope,
            "summary": res.summary,
            "entities_registered": res.entity_ids,
            "entities_indexed": res.pattern_count,
            "next_step": (
                "Run limem_pattern_put to attach a markdown profile to one of the entities."
                if res.entity_ids
                else ""
            ),
        },
        ensure_ascii=False,
    )


def _t_forget(event_id: str) -> str:
    eid = _resolve_event_id(event_id)
    res = do_forget(event_id=eid)
    return json.dumps(
        {
            "event_id": eid,
            "backend_action": (res.get("backend") or {}).get("action"),
            "local_rows_tombstoned": res.get("local_rows_tombstoned"),
        },
        ensure_ascii=False,
    )


def _t_list(types: list[str] | None = None, *, include_global: bool = True) -> str:
    project_id = detect_project_id()
    scopes = [f"project:{project_id}"] if project_id else []
    if include_global:
        scopes.append("global")
    idx = EntityIndex()
    metas = idx.list_hard_recall(
        allowed_scopes=scopes,
        allowed_types=types or ["rule", "feedback", "preference"],
    )
    return json.dumps(
        {
            "project_id": project_id,
            "items": [_serialize_event(m, source="list") for m in metas],
        },
        ensure_ascii=False,
    )


def _t_ping() -> str:
    creds = Credentials.load()
    if not creds.api_key:
        return json.dumps({"error": "no credentials; run `limem init` first"}, ensure_ascii=False)
    client = LimemClient(creds=creds)
    out: dict[str, Any] = {"base_url": creds.base_url, "db_id": creds.db_id}
    try:
        out["me"] = client.me()
        if creds.db_id:
            out["db_health"] = client.db_health()
    except LimemError as e:
        out["error"] = f"{e.status}: {e.message}"
    return json.dumps(out, ensure_ascii=False)


def _t_stats() -> str:
    return json.dumps(EntityIndex().stats(), ensure_ascii=False)


def _t_pause(
    *,
    duration_seconds: int = 3600,
    scope: str = "project",
    session_id: str | None = None,
) -> str:
    daemon_client.ensure_or_spawn()
    res = daemon_client.set_pause(
        duration_seconds=duration_seconds, scope=scope, session_id=session_id
    )
    if res is None:
        from .daemon.state import PauseState
        import time as _t
        until = int(_t.time()) + duration_seconds if duration_seconds > 0 else None
        p = PauseState(on=True, until_ts=until, scope=scope, session_id=session_id)
        p.save_to_disk()
        res = {"until_ts": until}
    return json.dumps({"paused": True, **(res or {})}, ensure_ascii=False)


def _t_resume() -> str:
    res = daemon_client.clear_pause()
    if res is None:
        from .daemon.state import PauseState
        PauseState(on=False).save_to_disk()
        res = {"ok": True}
    return json.dumps({"resumed": True, **(res or {})}, ensure_ascii=False)


def _t_fix(short_id: str, new_text: str) -> str:
    eid = _resolve_event_id(short_id)
    res = do_fix(event_id=eid, new_text=new_text)
    return json.dumps({"event_id": eid, **(res or {})}, ensure_ascii=False)


def _t_mute(short_id: str, session_id: str) -> str:
    session_mute.mute(session_id, short_id)
    return json.dumps(
        {"muted": True, "short_id": short_id.lstrip("#"), "session_id": session_id},
        ensure_ascii=False,
    )


# ---------- entity markdown 档案 ----------


def _t_pattern_get(entity_id: str) -> str:
    res = do_pattern_get(entity_id=entity_id)
    return json.dumps(res, ensure_ascii=False)


def _t_pattern_put(entity_id: str, content: str) -> str:
    if not (content or "").strip():
        return json.dumps({"error": "content must not be blank"}, ensure_ascii=False)
    res = do_pattern_put(entity_id=entity_id, content=content)
    return json.dumps(res, ensure_ascii=False)


def _t_pattern_delete(entity_id: str) -> str:
    res = do_pattern_delete(entity_id=entity_id)
    return json.dumps(res, ensure_ascii=False)


def main() -> None:
    import asyncio

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
