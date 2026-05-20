"""Hook 调度入口：``limem hook <tool> <event>``。

v3 重写要点（principal-centric）：
- UserPromptSubmit 拆为 **3 路完全并发**：hard / pattern（active principals 并发
  ``patterns_recall``）/ soft（BM25），独立超时、独立预算。
- SessionStart 跑 hard + pattern（对 active principals 用 "session start <project>
  <tool>" 查询拉档案切片），不跑 soft。
- 每次 hook 触发都会 lazy ``ensure_default_principals``（首次注册 user / agent /
  project），失败永远 swallow。
- SessionEnd / Codex Stop 缓冲池 / PostToolUse / PreCompact 行为保留。
- 失败永远 swallow（hook 不能阻塞用户 prompt）。
"""

from __future__ import annotations

import argparse
import json
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
    is_degraded_banner_emitted_on_disk,
    read_pause_from_disk,
)
from .daemon.writer import build_natural_detail
from .entity_index import EntityIndex, PrincipalRow
from .injector import (
    Budgets,
    InjectItem,
    PatternRecallSlice,
    hard_recall_to_items,
    pattern_recall_to_items,
    render_inject,
    render_inject_with_diagnostics,
    soft_recall_to_items,
)
from .principals import ensure_default_principals
from .redact import contains_secret
from .scope import detect_project_id, project_scope
from .tag_text import build_recall_query

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
    if not rendered:
        return
    try:
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
        # 上报失败永不阻塞 hook
        pass


# ---------- 召回辅助 ----------


def _allowed_scopes(project_id: str) -> list[str]:
    out = ["global"]
    if project_id:
        out.append(f"project:{project_id}")
    return out


def _safe_redact(text: str, patterns: list[str]) -> tuple[str, bool]:
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


def _patterns_recall_for_principals(
    principals: list[PrincipalRow],
    prompt: str,
    creds: Credentials,
    runtime: RuntimeConfig,
) -> list[PatternRecallSlice]:
    """对 active principals 并发拉取 markdown 切片。单 principal 超时即跳过。"""
    if not principals or not creds.api_key or not creds.db_id:
        return []

    per_timeout_s = max(0.02, runtime.patterns_recall_timeout_ms / 1000.0)
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
        if session_id and is_degraded_banner_emitted_on_disk(session_id):
            sys.stdout.write("")
        else:
            banner = _degraded_banner(reason)
            if session_id:
                _mark_degraded_emitted(session_id)
            _emit_inject("UserPromptSubmit", banner)
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

    idx = EntityIndex()
    scopes = _allowed_scopes(project_id)
    active_principals = _active_principals(
        idx, creds, project_id, tool, lazy_ensure=True
    )
    principal_id_set = {p.entity_id for p in active_principals}

    hard_metas = []
    pattern_slices: list[PatternRecallSlice] = []
    soft_results: list = []

    def _do_hard() -> None:
        nonlocal hard_metas
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

    def _do_soft() -> None:
        nonlocal soft_results
        if not creds.api_key or not creds.db_id:
            return
        client = LimemClient(creds=creds, timeout=runtime.hook_timeout_ms / 1000.0)
        try:
            q = build_recall_query(prompt)
            soft_results = client.query(q, top_k=runtime.bm25_query_top_k)
            daemon_client.set_connectivity(status=200, ok=True)
        except LimemError as e:
            daemon_client.set_connectivity(status=e.status, reason=str(e.message)[:60])
            _log("soft_recall_error", tool, status=e.status, msg=e.message)
        except Exception as e:  # noqa: BLE001
            daemon_client.set_connectivity(status=0, reason="network")
            _log("soft_recall_exc", tool, msg=str(e))

    hook_t = runtime.hook_timeout_ms / 1000.0
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_hard = pool.submit(_do_hard)
        f_pattern = pool.submit(_do_pattern)
        f_soft = pool.submit(_do_soft)
        for fut, label in ((f_hard, "hard"), (f_pattern, "pattern"), (f_soft, "soft")):
            try:
                fut.result(timeout=hook_t)
            except FutTimeout:
                _log(f"{label}_timeout", tool)

    soft_filtered = idx.filter_query_results(
        soft_results,
        allowed_scopes=set(scopes),
        excluded_types={"rule", "feedback", "preference"},
        allowed_principals=principal_id_set or None,
    )

    items = (
        hard_recall_to_items(hard_metas, idx=idx)
        + pattern_recall_to_items(pattern_slices)
        + soft_recall_to_items(soft_filtered, idx=idx)
    )

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
    if items:
        daemon_client.bump_hit(session_id)
    # 注入完成后 fire-and-forget 上报本轮实际渲染的 items（含 short_id），
    # 供 statusline / dash 显示「上轮 / 最近召回」反馈。失败永不阻塞。
    _report_recall_safe(
        rendered=rendered_items,
        session_id=session_id,
        project_id=project_id,
        scope=scope,
        prompt=prompt,
        via_patterns=via_patterns,
        via_keywords=via_keywords,
        injected_chars=len(text),
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
        soft_hits=len(soft_results),
        soft_filtered=len(soft_filtered),
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

    idx = EntityIndex()
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
    """渲染 `📚 LiMem · 本次使用 N 条记忆：#a3f #9b2 (+1)` 单行文案。

    输入是 daemon ``consume_pending_recall`` 返回的 dict（即一条 RecallEmittedRecord
    的序列化形态）。空 items 返回空串，由调用方决定是否发 systemMessage。
    """
    items = record.get("items") or []
    if not items:
        return ""
    n = len(items)
    short_ids = [it.get("short_id") for it in items if it.get("short_id")]
    head = short_ids[:2]
    if head:
        extra = len(short_ids) - len(head)
        sid_part = " ".join(f"#{s}" for s in head)
        if extra > 0:
            return f"📚 LiMem · 本次使用 {n} 条记忆：{sid_part} (+{extra})"
        if n > len(head):
            # 有 short_id 但 n > head；剩余可能是 pattern 无 short_id 条目
            return f"📚 LiMem · 本次使用 {n} 条记忆：{sid_part} (+{n - len(head)})"
        return f"📚 LiMem · 本次使用 {n} 条记忆：{sid_part}"
    # 全部 pattern（无 short_id）
    return f"📚 LiMem · 本次使用 {n} 条记忆（pattern 切片）"


def _emit_stop_systemmessage(text: str) -> None:
    """写一行 Claude Code Stop hook 协议要求的 JSON 到 stdout。

    协议参考（已经 claude-code-guide 验证）：
        {"decision": "allow", "systemMessage": "<text>", "suppressOutput": false}
    """
    if not text:
        # 无内容 → 不打扰，stdout 空字符串（Claude Code 不展示）
        sys.stdout.write("")
        return
    sys.stdout.write(
        json.dumps(
            {
                "decision": "allow",
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

    # 每轮 Codex Stop 也尝试给出「本次使用了哪些记忆」提示，与 Claude Code 端体验一致：
    # - stdout 输出 systemMessage JSON（与 Claude Code 协议同形态；Codex 若不识别则被忽略，无副作用）
    # - stderr 输出一行 ASCII fallback（多数 CLI 会把 stderr 显示给用户，作为 stdout 不被识别时的兜底）
    try:
        text = _stop_recall_message(sid if sid != "unknown" else "")
    except Exception:
        text = ""
    _emit_stop_systemmessage(text)
    if text:
        try:
            ascii_fallback = text.encode("ascii", errors="ignore").decode("ascii").strip()
            if ascii_fallback:
                sys.stderr.write(ascii_fallback + "\n")
        except Exception:
            pass


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
    ts = int(time.time())
    scope = project_scope()
    project_id = detect_project_id()
    source = f"{tool}:stop_flush"
    text = f"Codex session {sid}, {len(events)} turns"
    detail = f"first_turn_ts={events[0]['ts']} last_turn_ts={events[-1]['ts']}"
    summary_payload = {
        "limem_scope": scope,
        "limem_type": "session_summary",
        "project_id": project_id,
        "session_id": sid,
        "source": source,
        "importance": 0.3,
        "text": text,
        "detail": build_natural_detail(
            text=text,
            detail=detail,
            scope=scope,
            mem_type="session_summary",
            project_id=project_id,
            session_id=sid,
            source=source,
            timestamp=ts,
        ),
    }
    try:
        LimemClient(creds=creds).ingest(summary_payload, timestamp=ts)
        daemon_client.set_connectivity(status=200, ok=True)
        _log("stop_flush", tool, buffer=str(buf), turns=len(events))
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
        _log("hook_exception", args.tool, event=args.event, traceback=traceback.format_exc())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
