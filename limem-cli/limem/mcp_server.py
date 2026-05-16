"""LiMem MCP stdio server。

阶段 1-4 工具集：
- ``limem_search`` / ``limem_write`` / ``limem_forget`` / ``limem_list``
- ``limem_ping`` / ``limem_stats``
- ``limem_pause`` / ``limem_resume``     — 阶段 3
- ``limem_fix`` / ``limem_mute``         — 阶段 4
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import daemon_client, session_mute
from .client import LimemClient, LimemError
from .config import Credentials
from .memory_writer import EntitySpec, fix as do_fix, forget as do_forget, remember as do_remember
from .pattern_index import PatternIndex
from .scope import detect_project_id


server: Server = Server("limem")


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name="limem_search",
            description=(
                "Search LiMem long-term memory for the current user/project. "
                "Returns matched memories with their IDs, scope, type, and origin text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "include_types": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="limem_write",
            description=(
                "Persist a long-term memory. Use when user says 'remember X' / 'don't do Y' / 'always Z'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["project", "global", "session"], "default": "project"},
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
                                "role": {"type": "string", "enum": ["forbidden", "preferred", "neutral", "subject"]},
                                "patterns": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["canonical", "role", "patterns"],
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
                    "types": {"type": "array", "items": {"type": "string"}, "default": ["rule", "feedback", "preference"]},
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
            description="Show local SQLite cache statistics.",
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
                "or full event_id. Does NOT create a new event."
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
    """支持 #short_id 与完整 event_id。"""
    s = (input_id or "").strip()
    if s.startswith("#"):
        s = s[1:]
    pidx = PatternIndex()
    looked = pidx.lookup_event_by_short_id(s)
    return looked or input_id


def _t_search(query: str, *, top_k: int = 5, include_types: list[str] | None = None) -> str:
    creds = Credentials.load()
    project_id = detect_project_id()
    scopes = _allowed_scopes(project_id)
    pidx = PatternIndex()

    pattern_hits = pidx.search_patterns(query, allowed_scopes=scopes, limit=top_k * 2)
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
    soft_filtered = pidx.filter_query_results(
        soft_results,
        allowed_scopes=set(scopes),
        allowed_types=set(include_types) if include_types else None,
        excluded_types=excluded if not include_types else None,
    )

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in pattern_hits:
        if h.event_id in seen:
            continue
        meta = pidx.lookup_event(h.event_id)
        if meta is None:
            continue
        seen.add(h.event_id)
        out.append(_serialize_event(meta, source="pattern", trigger=h.content))
    for qr, meta in soft_filtered:
        if meta.event_id in seen:
            continue
        seen.add(meta.event_id)
        out.append(_serialize_event(meta, source="bm25", trigger=None, score=qr.score))
    return json.dumps({"matches": out[:top_k], "project_id": project_id}, ensure_ascii=False)


def _serialize_event(meta, *, source: str, trigger: str | None, score: float | None = None) -> dict[str, Any]:
    raw = meta.raw_metadata or {}
    pidx = PatternIndex()
    try:
        short = pidx.ensure_short_id(meta.event_id)
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
        "trigger_pattern": trigger,
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
                patterns=list(raw.get("patterns") or []),
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
            "patterns_indexed": res.pattern_count,
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
    pidx = PatternIndex()
    metas = pidx.list_hard_recall(
        allowed_scopes=scopes,
        allowed_types=types or ["rule", "feedback", "preference"],
    )
    return json.dumps(
        {
            "project_id": project_id,
            "items": [_serialize_event(m, source="list", trigger=None) for m in metas],
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
    return json.dumps(PatternIndex().stats(), ensure_ascii=False)


def _t_pause(
    *,
    duration_seconds: int = 3600,
    scope: str = "project",
    session_id: str | None = None,
) -> str:
    # 同时确保 daemon 启动（pause 不能掉链子）
    daemon_client.ensure_or_spawn()
    res = daemon_client.set_pause(
        duration_seconds=duration_seconds, scope=scope, session_id=session_id
    )
    if res is None:
        # daemon 不可达 fallback：直接写 pause.json
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
    return json.dumps({"muted": True, "short_id": short_id.lstrip("#"), "session_id": session_id}, ensure_ascii=False)


def main() -> None:
    import asyncio

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
