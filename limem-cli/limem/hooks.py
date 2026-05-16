"""Hook 调度入口：``limem hook <tool> <event>``。

阶段 1-8 累积改造：
- 每次入口都先读 ``pause.json``（直接读磁盘，不依赖 daemon）
- degraded banner 每 session 一次（去重经由 ``DEGRADED_SEEN_PATH``）
- 注入区块带 ``via=`` 与 ``#<short_id>``；session_mute 过滤
- 写 events.ndjson 新 schema（供 daemon 异步消费）
- 调 ``auto_init_project`` RPC（25ms 超时，非阻塞）
- ``bump_hit`` 命中召回后调 daemon 计数
- 失败永远 swallow
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path
from typing import Any

from . import daemon_client, session_mute
from .client import LimemClient, LimemError
from .config import (
    EVENTS_LOG_PATH,
    Credentials,
    ProjectConfig,
    RuntimeConfig,
    SESSIONS_DIR,
)
from .daemon.eventbus import emit_event
from .daemon.state import (
    is_degraded_banner_emitted_on_disk,
    read_pause_from_disk,
)
from .injector import (
    hard_recall_to_items,
    pattern_recall_to_items,
    render_inject,
    soft_recall_to_items,
)
from .pattern_index import PatternIndex
from .redact import contains_secret
from .scope import detect_project_id, project_scope
from .tag_text import build_recall_query


# ---------- 日志（兼容旧 _log 接口） ----------


def _log(event: str, tool: str, **fields: Any) -> None:
    # 旧 _log 已升级为事件总线写入；保留接口便于诊断输出
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


def _emit_inject(event_name: str, text: str) -> None:
    if not text:
        sys.stdout.write("")
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


# ---------- 召回 ----------


def _allowed_scopes(project_id: str) -> list[str]:
    out = ["global"]
    if project_id:
        out.append(f"project:{project_id}")
    return out


def _safe_redact(text: str, patterns: list[str]) -> tuple[str, bool]:
    """对 prompt 做 secret 过滤（用于 events.ndjson 隐私保护）。"""
    if not text:
        return "", False
    hit = contains_secret(text, patterns)
    if hit:
        return "[REDACTED]", True
    return text, False


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


def _degraded_banner(reason: str) -> str:
    return (
        f'<limem_memory status="degraded" reason="{reason}">\n'
        f'⚠️ LiMem 暂不可用 ({reason})。本轮无召回。诊断：`limem ping`\n'
        f'</limem_memory>'
    )


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

    # F1 自动 init：非阻塞 RPC（25ms 超时）
    try:
        daemon_client.safe_call("auto_init_project", {"cwd": str(Path.cwd())})
    except Exception:
        pass

    # F7 pause 优先
    pause = read_pause_from_disk()
    if pause.is_active():
        _log("user_prompt_submit_paused", tool, session_id=session_id, project_id=project_id, scope=scope)
        sys.stdout.write("")
        return

    # F9 degraded：本 session 首次注入 banner，其后仅 statusline
    conn = daemon_client.get_connectivity()
    if conn and conn.get("state") == "degraded":
        reason = conn.get("reason") or "unknown"
        if session_id and is_degraded_banner_emitted_on_disk(session_id):
            sys.stdout.write("")
        else:
            banner = _degraded_banner(reason)
            if session_id:
                # 通过 daemon 记录已注入；fallback 本地落盘
                try:
                    from .daemon.state import DEGRADED_SEEN_PATH  # noqa
                except Exception:
                    pass
                _mark_degraded_emitted(session_id)
            _emit_inject("UserPromptSubmit", banner)
        # degraded 期间也写 events 便于 daemon 学习器观察
        _emit_event_safe(
            "user_prompt_submit", tool, prompt, session_id, project_id, scope, runtime
        )
        return

    pidx = PatternIndex()
    scopes = _allowed_scopes(project_id)

    pattern_hits: list = []
    soft_results: list = []

    def _do_pattern() -> None:
        nonlocal pattern_hits
        pattern_hits = pidx.search_patterns(prompt, allowed_scopes=scopes, limit=8)

    def _do_soft() -> None:
        nonlocal soft_results
        if not creds.api_key or not creds.db_id:
            return
        client = LimemClient(creds=creds, timeout=runtime.hook_timeout_ms / 1000.0)
        try:
            q = build_recall_query(
                prompt, scopes=[], types=[],
                canonical_hints=[h.content for h in pattern_hits] if pattern_hits else None,
            )
            soft_results = client.query(q, top_k=runtime.bm25_query_top_k)
            daemon_client.set_connectivity(status=200, ok=True)
        except LimemError as e:
            daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
            _log("soft_recall_error", tool, status=e.status, msg=e.message)
        except Exception as e:  # noqa: BLE001
            daemon_client.set_connectivity(status=0, reason="network")
            _log("soft_recall_exc", tool, msg=str(e))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_do_pattern)
        try:
            f1.result(timeout=runtime.hook_timeout_ms / 1000.0)
        except FutTimeout:
            _log("pattern_timeout", tool)
        f2 = pool.submit(_do_soft)
        try:
            f2.result(timeout=runtime.hook_timeout_ms / 1000.0)
        except FutTimeout:
            _log("soft_timeout", tool)

    soft_filtered = pidx.filter_query_results(
        soft_results,
        allowed_scopes=set(scopes),
        excluded_types={"rule", "feedback", "preference"},
    )

    items = (
        pattern_recall_to_items(pattern_hits, pidx.lookup_event, pidx=pidx)
        + soft_recall_to_items(soft_filtered, pidx=pidx)
    )

    # session_mute 过滤
    muted = session_mute.get_muted(session_id) if session_id else set()
    if muted:
        items = [it for it in items if (it.short_id or it.event_id[:12]) not in muted]

    via_patterns = [h.content for h in pattern_hits[:3]]
    via_keywords = _via_keywords(prompt, limit=2)

    text = render_inject(
        items,
        project_id=project_id,
        total_budget=runtime.inject_budget_soft + runtime.inject_budget_hard,
        via_patterns=via_patterns,
        via_keywords=via_keywords,
    )
    if items:
        daemon_client.bump_hit(session_id)
    _log(
        "user_prompt_submit",
        tool,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        prompt_head=prompt[:60],
        pattern_hits=len(pattern_hits),
        soft_hits=len(soft_results),
        soft_filtered=len(soft_filtered),
        injected_chars=len(text),
    )
    _emit_event_safe(
        "user_prompt_submit", tool, prompt, session_id, project_id, scope, runtime
    )
    _emit_inject("UserPromptSubmit", text)


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
) -> None:
    safe_prompt, redacted = _safe_redact(prompt, runtime.redact_patterns)
    emit_event(
        kind,
        tool=tool,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        payload={"prompt": safe_prompt},
        redacted=redacted,
    )


def _hook_session_start(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    project_id = detect_project_id()
    scope = f"project:{project_id}" if project_id else "global"
    session_id = payload.get("session_id") or payload.get("sessionId") or ""

    # F1 自动 init（非阻塞）
    try:
        daemon_client.safe_call("auto_init_project", {"cwd": str(Path.cwd())})
    except Exception:
        pass

    pause = read_pause_from_disk()
    if pause.is_active():
        sys.stdout.write("")
        return

    pidx = PatternIndex()
    scopes = _allowed_scopes(project_id)
    metas = pidx.list_hard_recall(
        allowed_scopes=scopes,
        allowed_types=["rule", "feedback", "preference"],
    )
    items = hard_recall_to_items(metas, pidx=pidx)
    text = render_inject(
        items,
        project_id=project_id,
        total_budget=runtime.inject_budget_hard,
    )
    if items:
        daemon_client.bump_hit(session_id)
    _log(
        "session_start", tool,
        session_id=session_id, project_id=project_id, scope=scope,
        hard_recall=len(metas), injected_chars=len(text),
    )
    emit_event(
        "session_start", tool=tool, session_id=session_id,
        project_id=project_id, scope=scope, payload={},
    )
    _emit_inject("SessionStart", text)


def _hook_session_end(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    project_id = detect_project_id()
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    # session_mute 清理
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
    summary = {
        "limem_scope": project_scope(),
        "limem_type": "session_summary",
        "project_id": project_id,
        "session_id": session_id,
        "source": f"{tool}:hook:SessionEnd",
        "importance": 0.3,
        "text": payload.get("transcript_head", "")[:500],
        "detail": payload.get("detail") or "",
        "raw_payload_keys": list(payload.keys()),
    }
    client = LimemClient(creds=creds)
    try:
        res = client.ingest(summary)
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
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    buf = SESSIONS_DIR / f"{sid}.ndjson"
    with buf.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "payload": payload}, ensure_ascii=False) + "\n")

    now = int(time.time())
    threshold = now - runtime.codex_stop_idle_seconds
    for p in SESSIONS_DIR.glob("*.ndjson"):
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime >= threshold:
            continue
        _flush_codex_session(p, creds, tool)


def _flush_codex_session(buf: Path, creds: Credentials, tool: str) -> None:
    try:
        lines = buf.read_text().splitlines()
    except FileNotFoundError:
        return
    if not lines:
        buf.unlink(missing_ok=True)
        return
    events = [json.loads(line) for line in lines if line.strip()]
    sid = buf.stem
    summary_payload = {
        "limem_scope": project_scope(),
        "limem_type": "session_summary",
        "project_id": detect_project_id(),
        "session_id": sid,
        "source": f"{tool}:stop_flush",
        "importance": 0.3,
        "text": f"Codex session {sid}, {len(events)} turns",
        "detail": f"first_turn_ts={events[0]['ts']} last_turn_ts={events[-1]['ts']}",
    }
    try:
        LimemClient(creds=creds).ingest(summary_payload)
        daemon_client.set_connectivity(status=200, ok=True)
        _log("stop_flush", tool, buffer=str(buf), turns=len(events))
        # session_mute 清理
        session_mute.clear(sid)
        buf.unlink(missing_ok=True)
    except LimemError as e:
        daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
        _log("stop_flush_error", tool, msg=str(e), buffer=str(buf))
    except Exception as e:  # noqa: BLE001
        daemon_client.set_connectivity(status=0, reason="network")
        _log("stop_flush_error", tool, msg=str(e), buffer=str(buf))


def _hook_pre_compact(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    _log("pre_compact", tool, payload_keys=list(payload.keys()))


def _hook_post_tool_use(
    tool: str, payload: dict[str, Any], creds: Credentials, runtime: RuntimeConfig
) -> None:
    """F3 采集：编辑工具的 file_path/diff_summary/accepted 信号。

    Claude Code 在 Edit/Write/NotebookEdit 后会触发此 hook（若已订阅）；
    Codex 无此事件，由 UserPromptSubmit 中的 apply_patch 关键字降级推断。
    """
    project_id = detect_project_id()
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    file_path = payload.get("file_path") or ""
    accepted = bool(payload.get("accepted", True))  # Claude Code 默认应用即视为 accepted
    diff_summary = ""
    # 尽力提取 diff 摘要：Edit 提供 old_string/new_string；Write 提供 content
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
        elif args.event == "Stop":
            _log("stop_noop", args.tool)
        elif args.event == "PreCompact":
            _hook_pre_compact(args.tool, payload, creds, runtime)
        elif args.event == "PostToolUse":
            _hook_post_tool_use(args.tool, payload, creds, runtime)
    except Exception:
        _log("hook_exception", args.tool, event=args.event, traceback=traceback.format_exc())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
