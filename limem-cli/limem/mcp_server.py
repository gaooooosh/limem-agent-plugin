"""LiMem MCP stdio server。

v3 工具集（principal-centric）：
- ``limem_search`` ：对 active principals 并发 ``patterns_recall`` + BM25 软召回，三段聚合
- ``limem_write``  ：写一条 event；``entities`` 入参降级为 mention 元信息，**不再注册后端 entity**
- ``limem_forget`` ：归档 event
- ``limem_fix``    ：修改 event 文本（不动 principal markdown）
- ``limem_list``   ：列出本项目 + global 的强规则
- ``limem_pause`` / ``limem_resume`` / ``limem_mute`` ：保留
- ``limem_ping``  / ``limem_stats`` ：保留（stats 输出 principals 维度）
- ``limem_pattern_{get,put,delete}`` ：principal markdown CRUD，入参支持 alias
  (``"project" / "user" / "agent"``) 或 stable entity_id
- ``limem_principal_{list,register,activate,deactivate}`` ：principal 管理（v3 新增）
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import daemon_client, session_mute
from .client import LimemClient, LimemError
from .config import RECENT_RECALLS_PATH, Credentials, RuntimeConfig
from .entity_index import EntityIndex
from .memory_writer import (
    EntitySpec,
)
from .memory_writer import (
    delete_pattern as do_pattern_delete,
)
from .memory_writer import (
    fix as do_fix,
)
from .memory_writer import (
    forget as do_forget,
)
from .memory_writer import (
    get_pattern as do_pattern_get,
)
from .memory_writer import (
    remember as do_remember,
)
from .memory_writer import (
    update_pattern as do_pattern_put,
)
from .principals import (
    PrincipalSpec,
    ensure_current_user_principal,
    ensure_default_principals,
    entity_id_for,
    principal_alias_to_id,
    register_principal,
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
                "Returns matched memories (events) AND principal markdown sections "
                "(`pattern` source: user / agent / project profiles)."
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
                "'don't do Y' / 'always Z'. v3: `entities` is mention metadata only "
                "(canonicals + aliases) — it is NOT registered as a backend entity. "
                "To attach a markdown profile to user / agent / project, call "
                "`limem_pattern_put` with alias 'user' / 'agent' / 'project'."
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
                        "description": "Mentions (deprecated name). canonical / aliases only.",
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
            description="Show local SQLite cache statistics (principals/events/short_ids).",
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
                "or full event_id. Does NOT create a new event. For editing principal "
                "markdown profiles, use limem_pattern_put instead."
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
        # principal markdown CRUD（alias 解析支持 "project" / "user" / "agent"）
        Tool(
            name="limem_pattern_get",
            description=(
                "Fetch the entire markdown profile of a principal. "
                "`entity_id` accepts aliases 'project' / 'user' / 'agent' or a stable id."
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
                "Replace a principal's markdown profile (whole-document upsert). "
                "`entity_id` accepts aliases 'project' / 'user' / 'agent' or a stable id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "Full markdown content; non-empty. H2 sections are used for recall scoring.",
                    },
                },
                "required": ["entity_id", "content"],
            },
        ),
        Tool(
            name="limem_pattern_delete",
            description="Hard-delete a principal's markdown profile. The principal itself is NOT removed.",
            inputSchema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        ),
        # v3：principal 管理
        Tool(
            name="limem_principal_list",
            description="List active principals (user / agent / project / team / service).",
            inputSchema={
                "type": "object",
                "properties": {
                    "active_only": {"type": "boolean", "default": True},
                    "principal_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by type (user/agent/project/team/service).",
                    },
                },
            },
        ),
        Tool(
            name="limem_principal_register",
            description=(
                "Register a new principal (typically team / service). Default principals "
                "(user / agent / project) are auto-ensured on SessionStart and do not need this."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "principal_type": {
                        "type": "string",
                        "enum": ["user", "agent", "project", "team", "service"],
                    },
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": "string", "default": "global"},
                },
                "required": ["principal_type", "slug", "description"],
            },
        ),
        Tool(
            name="limem_principal_activate",
            description="Re-activate a deactivated principal so it participates in recall again.",
            inputSchema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        ),
        Tool(
            name="limem_principal_deactivate",
            description="Deactivate a principal: stop calling /patterns/recall on it. Markdown is preserved.",
            inputSchema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        ),
        Tool(
            name="limem_recent_recalls",
            description=(
                "Show which LiMem memories were injected into the most recent prompts. "
                "Use when the user asks things like 'what memory did you use just now?', "
                "'show me the last recall', '上一轮用了哪些记忆', or to audit which "
                "short_ids were active so you can reference them with /limem.fix or "
                "/limem.no. Returns newest-first; each record contains items with "
                "short_id / src (hard|pattern|bm25) / mem_type / scope / summary_head."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "current_project_only": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, filter records whose scope is the current "
                            "project or global (drops other-project recalls)."
                        ),
                    },
                },
                "required": [],
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
        elif name == "limem_principal_list":
            text = _t_principal_list(**arguments)
        elif name == "limem_principal_register":
            text = _t_principal_register(**arguments)
        elif name == "limem_principal_activate":
            text = _t_principal_activate(**arguments)
        elif name == "limem_principal_deactivate":
            text = _t_principal_deactivate(**arguments)
        elif name == "limem_recent_recalls":
            text = _t_recent_recalls(**arguments)
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


def _resolve_principal_id(alias_or_id: str, *, idx: EntityIndex | None = None) -> str:
    """alias → stable entity_id；不存在则返回原值（让下游 404 报错）。"""
    idx = idx or EntityIndex()
    creds = Credentials.load()
    project_id = detect_project_id()
    # 工具名未知（mcp 是单 server，无 hook 上下文）；用占位 "claude-code" 让 agent alias 也可解析
    tool = "claude-code"
    return principal_alias_to_id(
        alias_or_id, creds=creds, project_id=project_id, tool=tool, idx=idx
    )


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

    # 1) 对 active principals 并发 patterns_recall
    if include_patterns and creds.api_key and creds.db_id:
        try:
            ensure_default_principals(
                creds,
                project_id=project_id,
                tool="",
                idx=idx,
                include_user=True,
                include_agent=False,
                include_project=True,
            )
        except Exception:
            pass
        principals = idx.list_principals(active_only=True)
        client = LimemClient(
            creds=creds, timeout=runtime.patterns_recall_timeout_ms / 1000.0
        )
        for p in principals:
            try:
                res = client.patterns_recall(
                    p.entity_id,
                    query,
                    mode="section",
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
                    "entity_id": p.entity_id,
                    "principal_type": p.principal_type,
                    "canonical": p.canonical,
                    "scope": p.scope,
                    "mode": res.mode,
                    "matched_sections": [
                        {"heading": s.heading, "score": s.score} for s in res.matched_sections
                    ],
                    "content": res.content,
                    "source": "pattern",
                }
            )

    # 2) BM25 soft 召回
    soft_results = []
    principal_id_set = {p.entity_id for p in idx.list_principals(active_only=True)} or None
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
        allowed_principals=principal_id_set,
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
        "principal_ids": raw.get("principal_ids", []),
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
    next_step = (
        "If this should become a persistent profile, call limem_pattern_put with "
        "entity_id='project' / 'user' / 'agent'."
        if res.principal_ids
        else ""
    )
    return json.dumps(
        {
            "event_id": res.event_id,
            "scope": res.scope,
            "summary": res.summary,
            "principal_ids": res.principal_ids,
            "canonicals": res.canonicals,
            "next_step": next_step,
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
    principals = idx.list_principals(active_only=True)
    return json.dumps(
        {
            "project_id": project_id,
            "items": [_serialize_event(m, source="list") for m in metas],
            "principals": [
                {
                    "entity_id": p.entity_id,
                    "principal_type": p.principal_type,
                    "canonical": p.canonical,
                    "has_pattern": p.has_pattern,
                }
                for p in principals
            ],
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
        import time as _t

        from .daemon.state import PauseState
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


# ---------- principal markdown ----------


def _t_pattern_get(entity_id: str) -> str:
    eid = _resolve_principal_id(entity_id)
    res = do_pattern_get(entity_id=eid)
    return json.dumps({"alias_resolved_to": eid, **res}, ensure_ascii=False)


def _t_pattern_put(entity_id: str, content: str) -> str:
    if not (content or "").strip():
        return json.dumps({"error": "content must not be blank"}, ensure_ascii=False)
    eid = _resolve_principal_id(entity_id)
    res = do_pattern_put(entity_id=eid, content=content)
    return json.dumps({"alias_resolved_to": eid, **res}, ensure_ascii=False)


def _t_pattern_delete(entity_id: str) -> str:
    eid = _resolve_principal_id(entity_id)
    res = do_pattern_delete(entity_id=eid)
    return json.dumps({"alias_resolved_to": eid, **res}, ensure_ascii=False)


# ---------- principal 管理 ----------


def _t_principal_list(
    *,
    active_only: bool = True,
    principal_types: list[str] | None = None,
) -> str:
    idx = EntityIndex()
    rows = idx.list_principals(
        active_only=active_only, principal_types=principal_types or None
    )
    return json.dumps(
        {
            "principals": [
                {
                    "entity_id": r.entity_id,
                    "principal_type": r.principal_type,
                    "slug": r.slug,
                    "canonical": r.canonical,
                    "aliases": r.aliases,
                    "description": r.description,
                    "scope": r.scope,
                    "tool": r.tool,
                    "project_id": r.project_id,
                    "has_pattern": r.has_pattern,
                    "active": r.active,
                    "last_seen_ts": r.last_seen_ts,
                }
                for r in rows
            ]
        },
        ensure_ascii=False,
    )


def _t_principal_register(
    principal_type: str,
    slug: str,
    description: str,
    *,
    aliases: list[str] | None = None,
    scope: str = "global",
) -> str:
    creds = Credentials.load()
    idx = EntityIndex()
    ensured_user = ""
    try:
        ensured_user = ensure_current_user_principal(creds, idx=idx)
    except Exception:
        ensured_user = ""
    spec = PrincipalSpec(
        principal_type=principal_type,  # type: ignore[arg-type]
        slug=slug,
        description=description,
        aliases=list(aliases or []),
        scope=scope,
        canonical=f"{principal_type}:{slug}",
    )
    eid = register_principal(spec, creds=creds, idx=idx, swallow=False)
    return json.dumps(
        {"entity_id": eid, "registered": True, "ensured_user_principal_id": ensured_user},
        ensure_ascii=False,
    )


def _t_principal_activate(entity_id: str) -> str:
    idx = EntityIndex()
    eid = _resolve_principal_id(entity_id, idx=idx)
    idx.activate_principal(eid)
    return json.dumps({"entity_id": eid, "active": True}, ensure_ascii=False)


def _t_principal_deactivate(entity_id: str) -> str:
    idx = EntityIndex()
    eid = _resolve_principal_id(entity_id, idx=idx)
    idx.deactivate_principal(eid)
    return json.dumps({"entity_id": eid, "active": False}, ensure_ascii=False)


def _read_recent_recalls_fallback() -> list[dict[str, Any]]:
    """daemon 不可达时，从 ~/.cache/limem/recent_recalls.json 读快照。"""
    try:
        data = json.loads(RECENT_RECALLS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return list(records) if isinstance(records, list) else []


def _t_recent_recalls(
    *,
    limit: int = 5,
    current_project_only: bool = False,
) -> str:
    limit = max(1, min(int(limit or 5), 20))
    source = "daemon"
    records = daemon_client.list_recent_recalls(limit=limit)
    if records is None:
        records = _read_recent_recalls_fallback()
        source = "cache"
    if current_project_only:
        proj = detect_project_id()
        allowed = {"global"} | ({f"project:{proj}"} if proj else set())
        records = [r for r in records if (r.get("scope") or "") in allowed]
    # 截断到 limit（daemon 已截但 fallback 没截）
    records = list(records)[:limit]
    return json.dumps(
        {
            "source": source,
            "count": len(records),
            "records": records,
        },
        ensure_ascii=False,
    )


# 保留导入以便扩展模块复用（避免 lint 警告）
_ = entity_id_for


def main() -> None:
    import asyncio

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
