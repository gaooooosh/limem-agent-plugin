"""Hook 调度入口：``limem hook <tool> <event>``。

v3 重写要点（principal-centric）：
- UserPromptSubmit 拆为 **3 路完全并发**：hard / pattern（active principals 并发
  ``patterns_recall``）/ task（后端 ``/recall``），独立超时、独立预算。
- SessionStart 跑 hard + pattern（对 active principals 用 "session start <project>
  <tool>" 查询拉档案切片），不跑 soft。
- 每次 hook 触发都会 lazy ``ensure_default_principals``（首次注册 user / agent /
  project），失败永远 swallow。
- SessionEnd / Codex Stop 缓冲池 / PostToolUse / PreCompact 行为保留。
- 失败永远 swallow（hook 不能阻塞用户 prompt）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path
from typing import Any

from . import daemon_client, session_mute
from .client import LimemClient, LimemError
from .config import (
    EVENTS_LOG_PATH,
    SESSIONS_DIR,
    Credentials,
    ProjectConfig,
    RuntimeConfig,
)
from .daemon.eventbus import emit_event
from .daemon.state import (
    RecalledItem,
    is_degraded_banner_emitted_on_disk,
    read_pause_from_disk,
    recall_item_label,
)
from .daemon.writer import build_natural_detail
from .entity_index import EntityIndex, PrincipalRow
from .injector import (
    Budgets,
    InjectItem,
    PatternRecallSlice,
    hard_recall_to_items,
    pattern_recall_to_items,
    render_backend_recall,
    render_inject,
    render_inject_with_diagnostics,
)
from .principals import ensure_default_principals
from .redact import contains_secret
from .scope import detect_project_id, project_scope

_CODEX_SESSION_PROMPT_CHARS = 4000
_CODEX_SESSION_PACKET_CHARS = 12000
_CODEX_SESSION_EVIDENCE_LIMIT = 20
_CODEX_PACKET_CONTEXT_CHARS = 700
_CODEX_PACKET_USER_CHARS = 1800
_CODEX_PACKET_ASSISTANT_CHARS = 4200
_CODEX_PACKET_OTHER_CHARS = 2200
_MIN_HOOK_TIMEOUT_S = 1.5
_MIN_PATTERN_RECALL_TIMEOUT_S = 0.3


def _read_text_lossy(path: Path) -> str:
    with path.open("rb") as f:
        return f.read().decode("utf-8", errors="replace")


# ---------- 日志 ----------


def _log(event: str, tool: str, **fields: Any) -> None:
    try:
        EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG_PATH.open("a") as f:
            row = {
                "ts": int(time.time()),
                "kind": event,
                "tool": tool,
                "session_id": fields.pop("session_id", ""),
                "project_id": fields.pop("project_id", ""),
                "scope": fields.pop("scope", ""),
                "payload": fields,
                "redacted": False,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _emit_inject(
    event_name: str, text: str, *, system_message: str = ""
) -> None:
    if not text and not system_message:
        sys.stdout.write("")
        return
    payload: dict[str, Any] = {}
    if system_message:
        payload["systemMessage"] = system_message
        payload["suppressOutput"] = False
    if text:
        payload["hookSpecificOutput"] = {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _codex_visible_recall_context(recall_notice: str) -> str:
    """Fallback for Codex desktop, which may hide hook systemMessage output.

    Codex still passes UserPromptSubmit additionalContext to the model. When the
    desktop host does not render hook notices, this instruction makes the memory
    citation visible in the assistant's final response instead.
    """
    notice = (recall_notice or "").strip()
    if not notice:
        return ""
    return (
        "<limem_visible_notice>\n"
        "在本轮最终回复末尾单独追加下面这段 LiMem 引用提示，原样输出：\n"
        f"{notice}\n"
        "</limem_visible_notice>"
    )


def _report_recall_safe(
    *,
    rendered: list[InjectItem],
    session_id: str,
    project_id: str,
    scope: str,
    prompt: str,
    via_patterns: list[str] | None,
    via_keywords: list[str] | None,
    injected_chars: int,
) -> None:
    """fire-and-forget 上报「本轮实际渲染出去」的 items 给 daemon。

    永不抛、永不阻塞 hook；daemon 不可达静默放弃（statusline / dash 最多显示上次缓存）。
    """
    items_payload = _recall_items_payload(rendered)
    if not items_payload:
        return
    try:
        _report_recall_payload_safe(
            items_payload=items_payload,
            session_id=session_id,
            project_id=project_id,
            scope=scope,
            prompt=prompt,
            via_patterns=via_patterns,
            via_keywords=via_keywords,
            injected_chars=injected_chars,
        )
    except Exception:
        # 上报失败永不阻塞 hook
        pass


def _recall_items_payload(rendered: list[InjectItem]) -> list[dict[str, Any]]:
    items_payload: list[dict[str, Any]] = []
    for it in rendered:
        short = it.short_id or (it.event_id[:12] if it.event_id else "")
        # injector.render_line 里 src 文案对 soft 用 "soft"，但为统一展示
        # （README + statusline 习惯 "bm25"），转换一次
        src = "bm25" if it.kind == "soft" else it.kind
        if it.kind == "pattern":
            summary_head = (it.pattern_content or "").strip()[:60]
        else:
            summary_head = (it.summary or "").strip()[:60]
        items_payload.append(
            {
                "short_id": short,
                "event_id": it.event_id,
                "src": src,
                "mem_type": it.mem_type,
                "scope": it.scope,
                "summary_head": summary_head,
                "canonical": it.canonical,
                "heading": it.heading,
            }
        )
    return items_payload


def _task_recall_payload(task_text: str, *, scope: str) -> dict[str, Any] | None:
    body = (task_text or "").strip()
    if not body:
        return None
    return {
        "short_id": "",
        "event_id": "",
        "src": "task",
        "mem_type": "task_recall",
        "scope": scope,
        "summary_head": _task_recall_summary(body),
        "canonical": "",
        "heading": "",
    }


def _report_recall_payload_safe(
    *,
    items_payload: list[dict[str, Any]],
    session_id: str,
    project_id: str,
    scope: str,
    prompt: str,
    via_patterns: list[str] | None,
    via_keywords: list[str] | None,
    injected_chars: int,
    allow_empty: bool = False,
) -> None:
    if not items_payload and not allow_empty:
        return
    try:
        daemon_client.report_recall(
            {
                "ts": int(time.time()),
                "session_id": session_id,
                "project_id": project_id,
                "scope": scope,
                "items": items_payload,
                "via_patterns": list(via_patterns or []),
                "via_keywords": list(via_keywords or []),
                "prompt_head": (prompt or "")[:60],
                "injected_chars": int(injected_chars or 0),
            }
        )
    except Exception:
        pass


def _report_backend_recall_safe(
    *,
    task_text: str,
    session_id: str,
    project_id: str,
    scope: str,
    prompt: str,
    via_keywords: list[str] | None,
    injected_chars: int,
) -> None:
    """Report backend task recall so automatic recall can be audited/de-duped."""
    item = _task_recall_payload(task_text, scope=scope)
    if not item:
        return
    _report_recall_payload_safe(
        items_payload=[item],
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        prompt=prompt,
        via_patterns=[],
        via_keywords=via_keywords,
        injected_chars=injected_chars,
    )


# ---------- 召回辅助 ----------


def _allowed_scopes(project_id: str) -> list[str]:
    out = ["global"]
    if project_id:
        out.append(f"project:{project_id}")
    return out


def _open_entity_index(tool: str) -> EntityIndex | None:
    try:
        return EntityIndex()
    except Exception as e:  # noqa: BLE001
        _log("entity_index_unavailable", tool, err=str(e)[:160])
        return None


def _safe_redact(text: str, patterns: list[str]) -> tuple[str, bool]:
    if not text:
        return "", False
    hit = contains_secret(text, patterns)
    if hit:
        return "[REDACTED]", True
    return text, False


def _codex_session_buffer_path(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in session_id)
    return SESSIONS_DIR / f"{safe or 'unknown'}.ndjson"


def _codex_session_flush_state_path(buf: Path) -> Path:
    return buf.with_suffix(buf.suffix + ".flush.json")


def _codex_event_fingerprint(ev: dict[str, Any]) -> dict[str, Any]:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "ts": ev.get("ts"),
        "kind": ev.get("kind") or "event",
        "payload_hash": hashlib.sha256(payload_text.encode("utf-8")).hexdigest()[:16],
    }


def _codex_flush_first_event_ts(events: list[dict[str, Any]]) -> Any:
    return events[0].get("ts") if events else None


def _read_codex_flush_submitted_count(buf: Path, events: list[dict[str, Any]]) -> int:
    state_path = _codex_session_flush_state_path(buf)
    try:
        state = json.loads(state_path.read_text() or "{}")
    except Exception:
        return 0
    if not isinstance(state, dict):
        return 0
    if state.get("first_event_ts") != _codex_flush_first_event_ts(events):
        return 0
    try:
        count = int(state.get("submitted_line_count") or 0)
    except (TypeError, ValueError):
        return 0
    if count < 0 or count > len(events):
        return 0
    return count


def _codex_flush_state_status(buf: Path) -> str:
    try:
        state = json.loads(_codex_session_flush_state_path(buf).read_text() or "{}")
    except Exception:
        return ""
    if not isinstance(state, dict):
        return ""
    return str(state.get("status") or "")


def _write_codex_flush_state(
    buf: Path,
    *,
    session_id: str,
    submitted_line_count: int,
    events: list[dict[str, Any]],
    idempotency_key: str,
    status: str,
    error: str = "",
) -> None:
    state_path = _codex_session_flush_state_path(buf)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": session_id,
        "first_event_ts": _codex_flush_first_event_ts(events),
        "submitted_line_count": submitted_line_count,
        "last_idempotency_key": idempotency_key,
        "status": status,
        "updated_ts": int(time.time()),
    }
    if error:
        state["last_error"] = error[:240]
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _cleanup_codex_flush_files(buf: Path, submitted_line_count: int) -> None:
    try:
        current_count = 0
        for line in _read_text_lossy(buf).splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                current_count += 1
    except FileNotFoundError:
        current_count = 0
    if current_count <= submitted_line_count:
        buf.unlink(missing_ok=True)
        _codex_session_flush_state_path(buf).unlink(missing_ok=True)


def _codex_flush_idempotency_key(
    *,
    session_id: str,
    project_id: str,
    source: str,
    events: list[dict[str, Any]],
) -> str:
    body = {
        "session_id": session_id,
        "project_id": project_id,
        "source": source,
        "events": [_codex_event_fingerprint(ev) for ev in events],
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"codex-stop-flush:{digest}"


def _append_codex_session_observation(
    *,
    session_id: str,
    kind: str,
    payload: dict[str, Any],
    runtime: RuntimeConfig,
) -> None:
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "kind": kind,
            "payload": payload,
        }
        with _codex_session_buffer_path(session_id or "unknown").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _ = runtime


def _append_codex_user_prompt_observation(
    *,
    session_id: str,
    prompt: str,
    project_id: str,
    scope: str,
    runtime: RuntimeConfig,
) -> None:
    safe_prompt, redacted = _safe_redact(
        prompt[:_CODEX_SESSION_PROMPT_CHARS], runtime.redact_patterns
    )
    _append_codex_session_observation(
        session_id=session_id,
        kind="user_prompt",
        payload={
            "role": "user",
            "content": safe_prompt,
            "project_id": project_id,
            "scope": scope,
            "redacted": redacted,
        },
        runtime=runtime,
    )


def _append_codex_assistant_response_observation(
    *,
    session_id: str,
    response: str,
    runtime: RuntimeConfig,
    source: str,
) -> bool:
    safe_response, redacted = _safe_redact(response, runtime.redact_patterns)
    if not safe_response.strip():
        return False
    content_hash = hashlib.sha1(safe_response.encode("utf-8")).hexdigest()[:12]
    if _codex_session_has_assistant_hash(session_id, content_hash):
        return False
    _append_codex_session_observation(
        session_id=session_id,
        kind="assistant_response",
        payload={
            "role": "assistant",
            "content": safe_response,
            "source": source,
            "content_hash": content_hash,
            "redacted": redacted,
        },
        runtime=runtime,
    )
    return True


def _emit_assistant_evidence_safe(
    *,
    tool: str,
    session_id: str,
    response: str,
    runtime: RuntimeConfig,
    source: str,
) -> None:
    text = _clean_assistant_evidence(response, runtime)
    if not text:
        return
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    try:
        emit_event(
            "assistant_evidence",
            tool=tool,
            session_id=session_id,
            project_id=detect_project_id(),
            scope=project_scope(),
            payload={
                "content": text,
                "content_hash": content_hash,
                "source": source,
            },
            redacted=False,
        )
    except Exception:
        pass


def _clean_assistant_evidence(response: str, runtime: RuntimeConfig) -> str:
    text = _strip_limem_notice(response or "")
    safe, _redacted = _safe_redact(
        text[: runtime.passive_learning_assistant_evidence_chars],
        runtime.redact_patterns,
    )
    return " ".join(safe.split())


def _strip_limem_notice(text: str) -> str:
    body = text or ""
    body = re.sub(
        r"<limem_visible_notice>.*?</limem_visible_notice>",
        "",
        body,
        flags=re.DOTALL,
    )
    body = re.sub(
        r"<limem_memory\b[^>]*>.*?</limem_memory>",
        "",
        body,
        flags=re.DOTALL,
    )
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("> 📚 LiMem") or stripped.startswith("> - "):
            continue
        if stripped.startswith("> 本次引用") or stripped.startswith("> 本次未引用"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _codex_session_has_assistant_hash(session_id: str, content_hash: str) -> bool:
    if not session_id or not content_hash:
        return False
    try:
        p = _codex_session_buffer_path(session_id)
        for line in _read_text_lossy(p).splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") != "assistant_response":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("content_hash") == content_hash:
                return True
    except Exception:
        return False
    return False


def _via_keywords(prompt: str, *, limit: int = 2) -> list[str]:
    import re

    tokens = re.findall(r"[一-鿿\w]{2,}", prompt or "", re.UNICODE)
    seen: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in seen:
            continue
        seen.append(tl)
        if len(seen) >= limit:
            break
    return seen


def _inject_item_key(item: InjectItem) -> str:
    if item.event_id:
        return f"event:{item.event_id}"
    if item.short_id:
        return f"short:{item.short_id}"
    if item.kind == "pattern" and (item.canonical or item.heading):
        return f"pattern:{item.canonical}:{item.heading}"
    return ""


def _task_recall_key(text: str) -> str:
    head = _task_recall_summary(text)
    if not head:
        return ""
    return f"task:{head}"


def _task_recall_summary(text: str) -> str:
    return " ".join((text or "").split())[:60]


def _filter_seen_recall_items(
    items: list[InjectItem], *, session_id: str
) -> list[InjectItem]:
    """Drop memories already injected earlier in this session.

    Hard rules/feedback/preferences are intentionally not filtered. They are the
    durable instructions users expect to be re-applied and visibly cited on any
    relevant turn. Session de-dupe only suppresses lower-signal repeats such as
    pattern slices or other non-hard recall items.
    """
    if not session_id or not items:
        return items
    try:
        seen = daemon_client.seen_recall_keys(session_id)
    except Exception:
        seen = set()
    if not seen:
        return items
    out: list[InjectItem] = []
    local_seen: set[str] = set()
    for item in items:
        if item.kind == "hard":
            out.append(item)
            continue
        key = _inject_item_key(item)
        if key and (key in seen or key in local_seen):
            continue
        if key:
            local_seen.add(key)
        out.append(item)
    return out


def _filter_seen_task_recall(text: str, *, session_id: str) -> str:
    if not text or not session_id:
        return text
    key = _task_recall_key(text)
    if not key:
        return text
    try:
        seen = daemon_client.seen_recall_keys(session_id)
    except Exception:
        seen = set()
    return "" if key in seen else text


def _degraded_banner(reason: str) -> str:
    return (
        f'<limem_memory status="degraded" reason="{reason}">\n'
        f'⚠️ LiMem 暂不可用 ({reason})。本轮无召回。诊断：`limem ping`\n'
        f'</limem_memory>'
    )


def _hook_timeout_s(runtime: RuntimeConfig) -> float:
    return max(_MIN_HOOK_TIMEOUT_S, runtime.hook_timeout_ms / 1000.0)


def _pattern_recall_timeout_s(runtime: RuntimeConfig) -> float:
    return max(_MIN_PATTERN_RECALL_TIMEOUT_S, runtime.patterns_recall_timeout_ms / 1000.0)


def _probe_backend_recovery(creds: Credentials, runtime: RuntimeConfig) -> bool:
    """Best-effort degraded-state recovery check for transient network failures."""
    if not creds.api_key or not creds.db_id:
        return False
    timeout_s = _hook_timeout_s(runtime)
    try:
        LimemClient(creds=creds, timeout=timeout_s).db_health(timeout=timeout_s)
    except Exception:
        return False
    try:
        daemon_client.set_connectivity(status=200, ok=True)
    except Exception:
        pass
    return True


def _should_probe_degraded_reason(reason: str) -> bool:
    reason_l = (reason or "").lower()
    if not reason_l:
        return True
    return any(
        token in reason_l
        for token in ("network", "timeout", "timed out", "read operation", "connect")
    )


def _patterns_recall_for_principals(
    principals: list[PrincipalRow],
    prompt: str,
    creds: Credentials,
    runtime: RuntimeConfig,
) -> list[PatternRecallSlice]:
    """对 active principals 并发拉取 markdown 切片。单 principal 超时即跳过。"""
    if not principals or not creds.api_key or not creds.db_id:
        return []

    per_timeout_s = _pattern_recall_timeout_s(runtime)
    client = LimemClient(creds=creds, timeout=per_timeout_s)

    def _fetch(p: PrincipalRow) -> tuple[PrincipalRow, Any]:
        try:
            res = client.patterns_recall(
                p.entity_id,
                prompt,
                mode="section",
                top_k_sections=runtime.patterns_recall_top_k_sections,
                timeout=per_timeout_s,
            )
            return p, res
        except Exception:
            return p, None

    slices: list[PatternRecallSlice] = []
    workers = min(8, max(1, len(principals)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for p, res in pool.map(_fetch, principals):
            if res is None or not res.has_content():
                continue
            if res.matched_sections:
                head = res.matched_sections[0].heading or ""
                score = sum(s.score for s in res.matched_sections)
            else:
                head = ""
                score = 0.5
            label = f"{p.principal_type}:{p.canonical or p.slug}"
            slices.append(
                PatternRecallSlice(
                    entity_id=p.entity_id,
                    canonical=label,
                    heading=head,
                    content=res.content,
                    score=float(score),
                )
            )
    return slices


def _active_principals(
    idx: EntityIndex,
    creds: Credentials,
    project_id: str,
    tool: str,
    *,
    lazy_ensure: bool = True,
    include_agent: bool = True,
) -> list[PrincipalRow]:
    """读取 active principals；主 hook 每次幂等 ensure 默认主体。

    不能只在 principals 为空时 ensure：旧安装可能已有 project/team 但缺 user 或
    agent。include_agent 只应由主 Agent hook 传 True，daemon/MCP/sub-agent 路径不猜。
    """
    if lazy_ensure:
        try:
            ensure_default_principals(
                creds,
                project_id=project_id,
                tool=tool,
                idx=idx,
                client=None,
                include_user=True,
                include_agent=include_agent,
                include_project=True,
            )
        except Exception:
            pass
    try:
        return idx.list_principals(active_only=True)
    except Exception:
        return []


# ---------- UserPromptSubmit ----------


def _hook_user_prompt_submit(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    prompt = (
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("text")
        or ""
    )
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    project_id = detect_project_id()
    scope = f"project:{project_id}" if project_id else "global"
    if tool == "codex" and prompt and runtime.codex_session_observation_enabled:
        _append_codex_user_prompt_observation(
            session_id=session_id,
            prompt=prompt,
            project_id=project_id,
            scope=scope,
            runtime=runtime,
        )

    # A4.1：可选探测 transcript_path，提取上一条 assistant 回复 head，供 daemon 改进 is_correction 判定。
    # payload 是自由 dict，本字段挂在 payload["prev_assistant_head"] 内，**不**新增 events.ndjson envelope 字段。
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath") or ""
    prev_assistant_head = _read_prev_assistant_head(
        transcript_path, runtime.prev_assistant_chars, runtime
    )
    extra_payload: dict[str, Any] = {}
    if prev_assistant_head:
        extra_payload["prev_assistant_head"] = prev_assistant_head

    try:
        daemon_client.safe_call("auto_init_project", {"cwd": str(Path.cwd())})
    except Exception:
        pass

    pause = read_pause_from_disk()
    if pause.is_active():
        _log(
            "user_prompt_submit_paused",
            tool,
            session_id=session_id,
            project_id=project_id,
            scope=scope,
        )
        sys.stdout.write("")
        return

    conn = daemon_client.get_connectivity()
    if conn and conn.get("state") == "degraded":
        reason = conn.get("reason") or "unknown"
        if _should_probe_degraded_reason(reason) and _probe_backend_recovery(creds, runtime):
            conn = None
        else:
            if session_id and is_degraded_banner_emitted_on_disk(session_id):
                sys.stdout.write("")
            else:
                banner = _degraded_banner(reason)
                if session_id:
                    _mark_degraded_emitted(session_id)
                _emit_inject("UserPromptSubmit", "", system_message=banner)
            _emit_event_safe(
                "user_prompt_submit",
                tool,
                prompt,
                session_id,
                project_id,
                scope,
                runtime,
                extra_payload=extra_payload or None,
            )
            return

    idx = _open_entity_index(tool)
    scopes = _allowed_scopes(project_id)
    active_principals = (
        _active_principals(idx, creds, project_id, tool, lazy_ensure=True)
        if idx is not None
        else []
    )
    hard_metas = []
    pattern_slices: list[PatternRecallSlice] = []
    task_recall_text = ""

    def _do_hard() -> None:
        nonlocal hard_metas
        if idx is None:
            return
        hard_metas = idx.list_hard_recall(
            allowed_scopes=scopes,
            allowed_types=["rule", "feedback", "preference"],
            min_importance=runtime.hard_min_importance,
        )

    def _do_pattern() -> None:
        nonlocal pattern_slices
        if not active_principals:
            return
        pattern_slices = _patterns_recall_for_principals(
            active_principals, prompt, creds, runtime
        )

    def _do_task_recall() -> None:
        nonlocal task_recall_text
        if not creds.api_key or not creds.db_id:
            return
        recall_timeout_s = _hook_timeout_s(runtime)
        client = LimemClient(creds=creds, timeout=recall_timeout_s)
        try:
            task_recall = client.recall_for_task(
                prompt,
                limit=runtime.bm25_query_top_k,
                include_debug=False,
                timeout=recall_timeout_s,
            )
            task_recall_text = task_recall.prompt_text
            daemon_client.set_connectivity(status=200, ok=True)
        except LimemError as e:
            daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
            _log("task_recall_error", tool, status=e.status, msg=e.message)
        except Exception as e:  # noqa: BLE001
            daemon_client.set_connectivity(status=0, reason="network")
            _log("task_recall_exc", tool, msg=str(e))

    hook_t = _hook_timeout_s(runtime)
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_hard = pool.submit(_do_hard)
        f_pattern = pool.submit(_do_pattern)
        f_task = pool.submit(_do_task_recall)
        for fut, label in ((f_hard, "hard"), (f_pattern, "pattern"), (f_task, "task_recall")):
            try:
                fut.result(timeout=hook_t)
            except FutTimeout:
                _log(f"{label}_timeout", tool)

    items = []
    if idx is not None:
        items.extend(hard_recall_to_items(hard_metas, idx=idx))
    items.extend(pattern_recall_to_items(pattern_slices))
    items = _filter_seen_recall_items(items, session_id=session_id)

    muted = session_mute.get_muted(session_id) if session_id else set()
    if muted:
        items = [it for it in items if (it.short_id or it.event_id[:12]) not in muted]

    via_patterns = [p.canonical or p.slug for p in active_principals[:3]]
    via_keywords = _via_keywords(prompt, limit=2)

    budgets = Budgets(
        hard=runtime.inject_budget_hard,
        pattern=runtime.inject_budget_pattern,
        soft=runtime.inject_budget_soft,
    )
    text, rendered_items = render_inject_with_diagnostics(
        items,
        project_id=project_id,
        budgets=budgets,
        via_patterns=via_patterns,
        via_keywords=via_keywords,
    )
    task_recall_text = _filter_seen_task_recall(
        task_recall_text, session_id=session_id
    )
    backend_text = render_backend_recall(task_recall_text)
    if backend_text:
        text = "\n\n".join(part for part in (text, backend_text) if part)
    if items or backend_text:
        daemon_client.bump_hit(session_id)
    # 注入完成后 fire-and-forget 上报本轮实际渲染的 items（含 short_id）；
    # hard/pattern/task 合并成一次 record，避免 Stop 提示只看见最后一次来源。
    recall_payload = _recall_items_payload(rendered_items)
    if task_recall_text:
        task_payload = _task_recall_payload(task_recall_text, scope=scope)
        if task_payload:
            recall_payload.append(task_payload)
    recall_record = {
        "ts": int(time.time()),
        "session_id": session_id,
        "project_id": project_id,
        "scope": scope,
        "items": recall_payload,
        "via_patterns": via_patterns,
        "via_keywords": via_keywords,
        "prompt_head": (prompt or "")[:60],
        "injected_chars": len(text),
    }
    recall_notice = _format_prompt_recall_systemmessage(recall_record)
    _report_recall_payload_safe(
        items_payload=recall_payload,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        prompt=prompt,
        via_patterns=via_patterns,
        via_keywords=via_keywords,
        injected_chars=len(text),
        allow_empty=True,
    )
    _log(
        "user_prompt_submit",
        tool,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        prompt_head=prompt[:60],
        hard_hits=len(hard_metas),
        principals=len(active_principals),
        pattern_slices=len(pattern_slices),
        task_recall_chars=len(task_recall_text),
        injected_chars=len(text),
        rendered=len(rendered_items),
    )
    _emit_event_safe(
        "user_prompt_submit",
        tool,
        prompt,
        session_id,
        project_id,
        scope,
        runtime,
        extra_payload=extra_payload or None,
    )
    if tool == "codex" and recall_payload:
        visible_notice = _codex_visible_recall_context(recall_notice)
        if visible_notice:
            text = "\n\n".join(part for part in (text, visible_notice) if part)
    _emit_inject("UserPromptSubmit", text, system_message=recall_notice)


def _mark_degraded_emitted(session_id: str) -> None:
    from .config import DEGRADED_SEEN_PATH
    try:
        data = json.loads(DEGRADED_SEEN_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[session_id] = int(time.time())
    DEGRADED_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEGRADED_SEEN_PATH.with_suffix(DEGRADED_SEEN_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(DEGRADED_SEEN_PATH)


def _emit_event_safe(
    kind: str,
    tool: str,
    prompt: str,
    session_id: str,
    project_id: str,
    scope: str,
    runtime: RuntimeConfig,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    safe_prompt, redacted = _safe_redact(prompt, runtime.redact_patterns)
    out_payload: dict[str, Any] = {"prompt": safe_prompt}
    if extra_payload:
        # 仅合并 daemon 真消费的可选字段（如 prev_assistant_head）；
        # 不引入新的 envelope 字段（守 feedback #b94b0fa：限定 event schema 层不可扩）
        out_payload.update(extra_payload)
    emit_event(
        kind,
        tool=tool,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        payload=out_payload,
        redacted=redacted,
    )


def _read_prev_assistant_head(
    transcript_path: str, chars: int, runtime: RuntimeConfig
) -> str:
    """A4.1：从 Claude Code transcript JSONL tail ≤4KB 中提取最近一条 assistant 回复 head。

    JSONL 每行为独立 JSON 对象，反向逐行解析寻找 `type=="assistant"`。
    任何失败（文件不存在 / 编码 / 解析）静默返回空串；本函数限 50ms 自包含。
    """
    if not transcript_path:
        return ""
    try:
        p = Path(transcript_path).expanduser()
        if not p.is_file():
            return ""
        # 仅读尾部 4KB，避免大文件全量加载
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > 4096:
                f.seek(size - 4096)
            tail = f.read().decode("utf-8", errors="replace")
        # 反向遍历行，找最末 assistant 条目
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            content = obj.get("content") or obj.get("message", {}).get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # Claude API content blocks: 取所有 text 类型拼接
                parts: list[str] = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        t = blk.get("text") or ""
                        if t:
                            parts.append(t)
                text = " ".join(parts)
            if not text:
                continue
            safe, _redacted = _safe_redact(text[:chars], runtime.redact_patterns)
            return safe
    except Exception as e:  # noqa: BLE001
        _log("transcript_tail_failed", "claude-code", err=str(e)[:120])
    return ""


# ---------- SessionStart ----------


def _hook_session_start(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    project_id = detect_project_id()
    scope = f"project:{project_id}" if project_id else "global"
    session_id = payload.get("session_id") or payload.get("sessionId") or ""

    try:
        daemon_client.safe_call("auto_init_project", {"cwd": str(Path.cwd())})
    except Exception:
        pass

    pause = read_pause_from_disk()
    if pause.is_active():
        sys.stdout.write("")
        return

    idx = _open_entity_index(tool)
    if idx is None:
        _emit_inject("SessionStart", "")
        return
    scopes = _allowed_scopes(project_id)
    metas = idx.list_hard_recall(
        allowed_scopes=scopes,
        allowed_types=["rule", "feedback", "preference"],
        min_importance=runtime.hard_min_importance,
    )

    # 注册 / 刷新默认 principals（user / agent / project）
    active_principals = _active_principals(
        idx, creds, project_id, tool, lazy_ensure=True
    )
    pattern_slices: list[PatternRecallSlice] = []
    if active_principals and creds.api_key and creds.db_id:
        q = f"session start {project_id} {tool}".strip()
        try:
            pattern_slices = _patterns_recall_for_principals(
                active_principals, q, creds, runtime
            )
        except Exception:
            pattern_slices = []

    items = hard_recall_to_items(metas, idx=idx) + pattern_recall_to_items(pattern_slices)
    budgets = Budgets(
        hard=runtime.inject_budget_hard,
        pattern=runtime.inject_budget_pattern if pattern_slices else 0,
        soft=0,
    )
    text = render_inject(items, project_id=project_id, budgets=budgets)
    if items:
        daemon_client.bump_hit(session_id)
    _log(
        "session_start", tool,
        session_id=session_id, project_id=project_id, scope=scope,
        hard_recall=len(metas), principals=len(active_principals),
        pattern_slices=len(pattern_slices), injected_chars=len(text),
    )
    emit_event(
        "session_start", tool=tool, session_id=session_id,
        project_id=project_id, scope=scope, payload={},
    )
    _emit_inject("SessionStart", text)


# ---------- Stop hook：本轮回答结束后给用户提示用了哪些记忆 ----------


def _format_stop_recall_systemmessage(record: dict[str, Any]) -> str:
    """渲染本轮用户可见的 LiMem 引用提示。

    输入是 daemon ``consume_pending_recall`` 返回的 dict（即一条 RecallEmittedRecord
    的序列化形态）。空 items 返回空串，由调用方决定是否发 systemMessage。
    """
    items = record.get("items") or []
    if not items:
        return "> 📚 LiMem\n> 本次未引用记忆"

    def _item_label(it: dict[str, Any]) -> str:
        summary = _clean_recall_summary(str(it.get("summary_head") or ""))
        if not summary and str(it.get("src") or "") == "pattern":
            summary = " · ".join(
                part
                for part in (
                    str(it.get("canonical") or "").strip(),
                    str(it.get("heading") or "").strip(),
                )
                if part
            )
        if len(summary) > 96:
            summary = summary[:95] + "…"
        item = RecalledItem(
            short_id=str(it.get("short_id") or ""),
            event_id=str(it.get("event_id") or ""),
            src=str(it.get("src") or ""),
            mem_type=str(it.get("mem_type") or ""),
            scope=str(it.get("scope") or ""),
            summary_head=summary,
            canonical=str(it.get("canonical") or ""),
            heading=str(it.get("heading") or ""),
        )
        return recall_item_label(item, max_chars=120) or "已匹配记忆"

    n = len(items)
    head_items = items[:4]
    detail = "\n".join(f"> - {_item_label(it)}" for it in head_items)
    extra = n - len(head_items)
    suffix = f"\n> - 另 {extra} 条" if extra > 0 else ""
    return f"> 📚 LiMem\n> 本次引用 {n} 条记忆\n{detail}{suffix}"


def _clean_recall_summary(text: str) -> str:
    """Strip metadata-ish tails from summaries before showing them to users."""
    summary = " ".join((text or "").split())
    summary = re.sub(r"[（(]\s*实体[:：].*$", "", summary).strip()
    return summary.rstrip("；;，,")


def _format_prompt_recall_systemmessage(record: dict[str, Any]) -> str:
    """User-visible recall notice emitted by UserPromptSubmit.

    This is intentionally not injected into ``additionalContext``. Hosts that
    surface top-level hook ``systemMessage`` can show it as a separate hook/tool
    style notice while the model only receives the actual memory context.
    """
    return _format_stop_recall_systemmessage(record)


def _emit_stop_systemmessage(text: str) -> None:
    """写一行 Claude Code Stop hook 协议要求的 JSON 到 stdout。

    Stop hook 只输出通用 top-level 字段；``decision`` 仅允许 approve/block，
    不能使用 PreToolUse 风格的 allow。
    """
    if not text:
        # 无内容 → 不打扰，stdout 空字符串（Claude Code 不展示）
        sys.stdout.write("")
        return
    sys.stdout.write(
        json.dumps(
            {
                "systemMessage": text,
                "suppressOutput": False,
            },
            ensure_ascii=False,
        )
    )


def _stop_recall_message(session_id: str) -> str:
    """从 daemon 取出本 session 待消费的 record 并渲染。
    静默条件：(a) 无 session_id (b) pause 中 (c) daemon 返回 None
    （daemon 内部已处理 dedupe 与"已消费"标记）。
    """
    if not session_id:
        return ""
    if read_pause_from_disk().is_active():
        return ""
    try:
        rec = daemon_client.consume_pending_recall(session_id, dedupe=True)
    except Exception:
        return ""
    if not rec:
        return ""
    return _format_stop_recall_systemmessage(rec)


def _hook_stop_claude(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    """Claude Code Stop hook：每轮回答结束时输出一行 systemMessage 提示本次使用的记忆。

    永不阻塞回答；任何异常均 swallow，输出空字符串。
    """
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    try:
        text = _stop_recall_message(session_id)
    except Exception:
        text = ""
    _emit_stop_systemmessage(text)
    _log(
        "stop_claude",
        tool,
        session_id=session_id,
        emitted=bool(text),
    )


# ---------- SessionEnd / Stop / Misc ----------


def _hook_session_end(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    project_id = detect_project_id()
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    if session_id:
        session_mute.clear(session_id)
    emit_event(
        "session_end", tool=tool, session_id=session_id,
        project_id=project_id, scope=f"project:{project_id}" if project_id else "global",
        payload={"keys": list(payload.keys())},
    )

    if not creds.api_key or not creds.db_id:
        _log("session_end_skipped", tool, reason="no creds")
        return
    ts = int(time.time())
    scope = project_scope()
    source = f"{tool}:hook:SessionEnd"
    text = payload.get("transcript_head", "")[:500]
    summary = {
        "limem_scope": scope,
        "limem_type": "session_summary",
        "project_id": project_id,
        "session_id": session_id,
        "source": source,
        "importance": 0.3,
        "text": text,
        "detail": build_natural_detail(
            text=text,
            detail=payload.get("detail") or text,
            scope=scope,
            mem_type="session_summary",
            project_id=project_id,
            session_id=session_id,
            source=source,
            timestamp=ts,
        ),
        "raw_payload_keys": list(payload.keys()),
    }
    client = LimemClient(creds=creds)
    try:
        res = client.ingest(summary, timestamp=ts)
        daemon_client.set_connectivity(status=200, ok=True)
        _log("session_end", tool, event_id=res.event_id)
    except LimemError as e:
        daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
        _log("session_end_error", tool, msg=str(e))
    except Exception as e:  # noqa: BLE001
        daemon_client.set_connectivity(status=0, reason="network")
        _log("session_end_error", tool, msg=str(e))


def _hook_stop_codex(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    sid = payload.get("session_id") or payload.get("sessionId") or "unknown"
    if runtime.codex_session_observation_enabled:
        assistant_response = _assistant_response_from_payload(payload)
        assistant_source = "stop_payload"
        if not assistant_response and sid != "unknown":
            assistant_response = _read_codex_last_assistant_response(sid, runtime)
            assistant_source = "codex_rollout"
        if assistant_response:
            _append_codex_assistant_response_observation(
                session_id=sid,
                response=assistant_response,
                runtime=runtime,
                source=assistant_source,
            )
        _append_codex_session_observation(
            session_id=sid,
            kind="stop",
            payload={"hook": "Stop"},
            runtime=runtime,
        )
    else:
        assistant_response = _assistant_response_from_payload(payload)
        assistant_source = "stop_payload"
        if not assistant_response and sid != "unknown":
            assistant_response = _read_codex_last_assistant_response(sid, runtime)
            assistant_source = "codex_rollout"
        if assistant_response and sid != "unknown":
            _emit_assistant_evidence_safe(
                tool=tool,
                session_id=sid,
                response=assistant_response,
                runtime=runtime,
                source=assistant_source,
            )

    # Emit the visible recall notice before passive session flushing. The flush
    # path can hit backend/network timeouts; recall visibility must stay on the
    # fast Stop-hook path.
    try:
        text = _stop_recall_message(sid if sid != "unknown" else "")
    except Exception:
        text = ""
    _emit_stop_systemmessage(text)
    if text:
        try:
            sys.stderr.write(text.strip() + "\n")
        except Exception:
            pass

    if runtime.codex_session_observation_enabled:
        now = int(time.time())
        threshold = now - runtime.codex_stop_idle_seconds
        for p in SESSIONS_DIR.glob("*.ndjson"):
            try:
                mtime = p.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime >= threshold:
                continue
            try:
                _flush_codex_session(p, creds, tool)
            except Exception as e:  # noqa: BLE001
                _log("stop_flush_error", tool, msg=str(e), buffer=str(p))
    else:
        _log(
            "stop_flush_skipped",
            tool,
            session_id=sid,
            reason="codex_session_observation_disabled",
        )


def _assistant_response_from_payload(payload: dict[str, Any]) -> str:
    for key in (
        "assistant_response",
        "assistantResponse",
        "response",
        "message",
        "content",
        "text",
    ):
        value = payload.get(key)
        text = _extract_text_from_unknown(value)
        if text:
            return text
    return ""


def _read_codex_last_assistant_response(session_id: str, runtime: RuntimeConfig) -> str:
    try:
        path = _find_codex_rollout_path(session_id)
        if not path:
            return ""
        return _read_last_assistant_from_codex_rollout(path, runtime)
    except Exception as e:  # noqa: BLE001
        _log("codex_assistant_capture_failed", "codex", session_id=session_id, err=str(e)[:120])
        return ""


def _find_codex_rollout_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    base = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    candidates = list((base / "sessions").glob(f"**/rollout-*{session_id}.jsonl"))
    if not candidates:
        candidates = list((base / "archived_sessions").glob(f"rollout-*{session_id}.jsonl"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[0]


def _read_last_assistant_from_codex_rollout(path: Path, runtime: RuntimeConfig) -> str:
    try:
        lines = _read_text_lossy(path).splitlines()
    except FileNotFoundError:
        return ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _assistant_text_from_codex_rollout_row(row)
        if not text:
            continue
        safe, _redacted = _safe_redact(text, runtime.redact_patterns)
        return safe
    return ""


def _assistant_text_from_codex_rollout_row(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    typ = row.get("type") or payload.get("type") or ""
    if typ == "event_msg" and payload.get("type") == "agent_message":
        return _extract_text_from_unknown(payload.get("message"))
    if typ == "response_item":
        if payload.get("role") == "assistant":
            return _extract_text_from_unknown(payload.get("content"))
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            return _extract_text_from_unknown(payload.get("content"))
    return ""


def _extract_text_from_unknown(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("text", "message", "content", "output_text"):
            text = _extract_text_from_unknown(value.get(key))
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(value, list):
        parts = [_extract_text_from_unknown(v) for v in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _markdown_escape(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _neutral_pack_text(text: str, budget: int) -> tuple[str, dict[str, Any]]:
    body = _markdown_escape(text)
    if budget <= 0:
        return "", {
            "truncated": bool(body),
            "original_chars": len(body),
            "kept_head_chars": 0,
            "kept_tail_chars": 0,
        }
    if len(body) <= budget:
        return body, {
            "truncated": False,
            "original_chars": len(body),
            "kept_head_chars": len(body),
            "kept_tail_chars": 0,
        }
    head_budget = budget // 2
    tail_budget = budget - head_budget
    packed = (
        body[:head_budget].rstrip()
        + "\n\n[... neutral truncation: middle omitted ...]\n\n"
        + body[-tail_budget:].lstrip()
    )
    return packed, {
        "truncated": True,
        "original_chars": len(body),
        "kept_head_chars": head_budget,
        "kept_tail_chars": tail_budget,
    }


def _packet_budget_for_kind(kind: str) -> int:
    if kind == "user_prompt":
        return _CODEX_PACKET_USER_CHARS
    if kind == "assistant_response":
        return _CODEX_PACKET_ASSISTANT_CHARS
    return _CODEX_PACKET_OTHER_CHARS


def _event_raw_ref(ev: dict[str, Any], kind: str, ordinal: int) -> str:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    ref = str(payload.get("content_hash") or payload.get("raw_ref") or "")
    if ref:
        return f"{kind}:{ref}"
    ts = str(ev.get("ts") or "unknown")
    return f"{kind}:{ordinal}:{ts}"


def _build_codex_evidence_packet(
    events: list[dict[str, Any]],
    *,
    project_id: str,
    tool: str,
    source: str,
) -> str:
    lines = ["# Agent Observation Packet", "", "## Context"]
    truncations: list[dict[str, Any]] = []
    raw_refs: list[str] = []
    if project_id:
        lines.append(f"- Project: {project_id}")
    lines.append(f"- Tool: {tool}")
    lines.append(f"- Source: {source}")
    lines.append("- Packing: neutral head/tail budget; no semantic summary")
    context_text, context_meta = _neutral_pack_text("\n".join(lines), _CODEX_PACKET_CONTEXT_CHARS)
    lines = context_text.splitlines()
    if context_meta["truncated"]:
        truncations.append({"ref": "context", **context_meta})
    lines.extend(["", "## Evidence Timeline"])

    evidence_count = 0
    for ev in events:
        kind = ev.get("kind") or "event"
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if kind == "user_prompt":
            content, meta = _neutral_pack_text(
                str(payload.get("content") or ""), _packet_budget_for_kind(kind)
            )
            if not content:
                continue
            evidence_count += 1
            ref = _event_raw_ref(ev, kind, evidence_count)
            raw_refs.append(ref)
            if meta["truncated"]:
                truncations.append({"ref": ref, **meta})
            lines.extend(
                [
                    "",
                    f"### {evidence_count}. User Message",
                    f"Ref: {ref}",
                    "",
                    content,
                ]
            )
        elif kind == "assistant_response":
            content, meta = _neutral_pack_text(
                str(payload.get("content") or ""), _packet_budget_for_kind(kind)
            )
            if not content:
                continue
            evidence_count += 1
            ref = _event_raw_ref(ev, kind, evidence_count)
            raw_refs.append(ref)
            if meta["truncated"]:
                truncations.append({"ref": ref, **meta})
            lines.extend(
                [
                    "",
                    f"### {evidence_count}. Assistant Response",
                    f"Ref: {ref}",
                    "",
                    content,
                ]
            )
        elif kind == "stop":
            evidence_count += 1
            ref = _event_raw_ref(ev, kind, evidence_count)
            raw_refs.append(ref)
            lines.extend(
                [
                    "",
                    f"### {evidence_count}. Stop Hook",
                    f"Ref: {ref}",
                    "",
                    "Codex emitted a Stop hook for this session.",
                ]
            )
        if evidence_count >= _CODEX_SESSION_EVIDENCE_LIMIT:
            break

    if evidence_count == 0:
        lines.extend(["", "No conversation evidence was captured before this stop event."])
    lines.extend(["", "## Truncation"])
    if truncations:
        lines.append(
            "Some evidence was neutrally truncated by fixed budget. If evidence is insufficient, return no-memory."
        )
        for item in truncations:
            lines.append(
                "- {ref}: original={original_chars}, kept_head={kept_head_chars}, "
                "kept_tail={kept_tail_chars}".format(**item)
            )
    else:
        lines.append("None")
    if raw_refs:
        lines.extend(["", "## Raw References"])
        lines.extend(f"- {ref}" for ref in raw_refs[:_CODEX_SESSION_EVIDENCE_LIMIT])

    packet = "\n".join(lines).strip()
    if len(packet) > _CODEX_SESSION_PACKET_CHARS:
        packet = packet[: _CODEX_SESSION_PACKET_CHARS - 20].rstrip() + "\n\n[truncated]"
    return packet


def _flush_codex_session(buf: Path, creds: Credentials, tool: str) -> None:
    try:
        lines = _read_text_lossy(buf).splitlines()
    except FileNotFoundError:
        return
    lines = [line for line in lines if line.strip()]
    if not lines:
        buf.unlink(missing_ok=True)
        _codex_session_flush_state_path(buf).unlink(missing_ok=True)
        return
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    if not events:
        _log("stop_flush_skipped", tool, reason="no_valid_events", buffer=str(buf))
        return
    sid = buf.stem
    submitted_line_count = _read_codex_flush_submitted_count(buf, events)
    if submitted_line_count >= len(events):
        if _codex_flush_state_status(buf) == "committed":
            _cleanup_codex_flush_files(buf, submitted_line_count)
        return
    flush_events = events[submitted_line_count:]
    ts = int(time.time())
    scope = project_scope()
    project_id = detect_project_id()
    source = f"{tool}:stop_flush"
    text = "Codex conversation evidence packet"
    detail = _build_codex_evidence_packet(
        flush_events,
        project_id=project_id,
        tool=tool,
        source=source,
    )
    idempotency_key = _codex_flush_idempotency_key(
        session_id=sid,
        project_id=project_id,
        source=source,
        events=flush_events,
    )
    summary_payload = {
        "limem_scope": scope,
        "limem_type": "session_observation",
        "project_id": project_id,
        "session_id": sid,
        "source": source,
        "idempotency_key": idempotency_key,
        "importance": 0.3,
        "text": text,
        "detail": detail,
        "metadata": {
            "turn_count": len(flush_events),
            "first_event_ts": flush_events[0].get("ts"),
            "last_event_ts": flush_events[-1].get("ts"),
            "event_start_index": submitted_line_count,
            "event_end_index": len(events),
            "buffer_line_count": len(events),
            "idempotency_key": idempotency_key,
            "packet_format": "turn_observation_neutral_pack_v1",
            "packet_budget_chars": _CODEX_SESSION_PACKET_CHARS,
            "evidence_limit": _CODEX_SESSION_EVIDENCE_LIMIT,
        },
    }
    try:
        client = LimemClient(creds=creds)
        try:
            ensure_default_principals(
                creds,
                project_id=project_id,
                tool=tool,
                idx=EntityIndex(),
                client=client,
                include_user=True,
                include_agent=True,
                include_project=True,
            )
        except Exception:
            pass
        client.ingest(summary_payload, timestamp=ts)
        _write_codex_flush_state(
            buf,
            session_id=sid,
            submitted_line_count=len(events),
            events=events,
            idempotency_key=idempotency_key,
            status="committed",
        )
        daemon_client.set_connectivity(status=200, ok=True)
        _log(
            "stop_flush",
            tool,
            buffer=str(buf),
            turns=len(flush_events),
            submitted_line_count=len(events),
            idempotency_key=idempotency_key,
        )
        session_mute.clear(sid)
        _cleanup_codex_flush_files(buf, len(events))
    except LimemError as e:
        _write_codex_flush_state(
            buf,
            session_id=sid,
            submitted_line_count=len(events),
            events=events,
            idempotency_key=idempotency_key,
            status="uncertain",
            error=str(e),
        )
        daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
        _log("stop_flush_error", tool, msg=str(e), buffer=str(buf))
    except Exception as e:  # noqa: BLE001
        _write_codex_flush_state(
            buf,
            session_id=sid,
            submitted_line_count=len(events),
            events=events,
            idempotency_key=idempotency_key,
            status="uncertain",
            error=str(e),
        )
        daemon_client.set_connectivity(status=0, reason="network")
        _log("stop_flush_error", tool, msg=str(e), buffer=str(buf))


def _hook_pre_compact(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    _log("pre_compact", tool, payload_keys=list(payload.keys()))


def _hook_pre_tool_use(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    """A1.1：仅 Edit/Write/NotebookEdit 三种工具采集 intent_summary（new_string 头部，经脱敏）。

    Bash 等工具不读 command head（隐私面），payload 只携带 tool + file_path 用于 daemon 配对。
    """
    project_id = detect_project_id()
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    file_path = payload.get("file_path") or ""
    out_payload: dict[str, Any] = {"tool": tool_name, "file_path": file_path}
    if tool_name in {"Edit", "Write", "NotebookEdit"}:
        intent_raw = (
            payload.get("new_string")
            or payload.get("content")
            or payload.get("new_source")
            or ""
        )
        if intent_raw:
            head = intent_raw[: runtime.pre_tool_intent_chars]
            safe_head, _redacted = _safe_redact(head, runtime.redact_patterns)
            out_payload["intent_summary"] = safe_head
    emit_event(
        "pre_tool_use",
        tool=tool,
        session_id=session_id,
        project_id=project_id,
        scope=f"project:{project_id}" if project_id else "global",
        payload=out_payload,
    )


def _hook_post_tool_use(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    project_id = detect_project_id()
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    file_path = payload.get("file_path") or ""
    accepted = bool(payload.get("accepted", True))
    diff_summary = ""
    if "new_string" in payload or "old_string" in payload:
        diff_summary = (
            f"old: {(payload.get('old_string') or '')[:200]} | "
            f"new: {(payload.get('new_string') or '')[:200]}"
        )[:400]
    elif "content" in payload:
        diff_summary = (payload.get("content") or "")[:400]
    elif "new_source" in payload:
        diff_summary = (payload.get("new_source") or "")[:400]

    emit_event(
        "post_tool_use",
        tool=tool,
        session_id=session_id,
        project_id=project_id,
        scope=f"project:{project_id}" if project_id else "global",
        payload={
            "tool": tool_name,
            "file_path": file_path,
            "accepted": accepted,
            "diff_summary": diff_summary,
        },
    )


# ---------- 入口 ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="limem hook")
    parser.add_argument("tool", choices=["claude-code", "codex"])
    parser.add_argument(
        "event",
        choices=[
            "UserPromptSubmit",
            "SessionStart",
            "SessionEnd",
            "Stop",
            "PreCompact",
            "PreToolUse",
            "PostToolUse",
        ],
    )
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    creds = Credentials.load()
    runtime = RuntimeConfig.load()
    project_cfg = ProjectConfig.discover()
    if project_cfg and project_cfg.enabled_hooks and args.event not in project_cfg.enabled_hooks:
        _log("event_disabled_by_project", args.tool, event=args.event)
        return 0

    try:
        if args.event == "UserPromptSubmit":
            _hook_user_prompt_submit(args.tool, payload, creds, runtime)
        elif args.event == "SessionStart":
            _hook_session_start(args.tool, payload, creds, runtime)
        elif args.event == "SessionEnd":
            _hook_session_end(args.tool, payload, creds, runtime)
        elif args.event == "Stop" and args.tool == "codex":
            _hook_stop_codex(args.tool, payload, creds, runtime)
        elif args.event == "Stop" and args.tool == "claude-code":
            _hook_stop_claude(args.tool, payload, creds, runtime)
        elif args.event == "Stop":
            _log("stop_noop", args.tool)
        elif args.event == "PreCompact":
            _hook_pre_compact(args.tool, payload, creds, runtime)
        elif args.event == "PreToolUse":
            _hook_pre_tool_use(args.tool, payload, creds, runtime)
        elif args.event == "PostToolUse":
            _hook_post_tool_use(args.tool, payload, creds, runtime)
    except Exception:
        _log("hook_exception", args.tool, hook_event=args.event, traceback=traceback.format_exc())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
