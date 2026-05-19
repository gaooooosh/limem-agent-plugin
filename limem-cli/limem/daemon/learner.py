"""被动学习器：周期性扫 events.ndjson 生成 suggestions。

F2 (correction 聚合): 中英纠正关键词 → trigram Jaccard → 候选 rule
F3 (N-gram preference): PostToolUse 接受率 → 候选 preference
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from ..config import (
    SUGGESTIONS_ARCHIVE_PATH,
    SUGGESTIONS_PATH,
)
from .jaccard import cluster_by_similarity, jaccard, trigrams
from .ngram import aggregate as ngram_aggregate

_CN_CORRECT = re.compile(r"不对|应该|别|不要|改成|纠正|换成|不是")
_EN_CORRECT = re.compile(r"don'?t|actually|stop|wrong|instead|prefer", re.IGNORECASE)
_NP_EXTRACT = re.compile(r"([一-鿿\w]{2,8})(?=\s*(?:不对|要|改成|换成|换))")
_NEGATIVE_HINT = re.compile(r"不对|别|不要|不是|don'?t|stop|wrong", re.IGNORECASE)
_PREFER_HINT = re.compile(r"应该|改成|换成|prefer|instead|actually", re.IGNORECASE)


def is_correction(text: str) -> bool:
    """旧 API：仅判断是否含纠正关键词（保持向后兼容；新代码改用 score_correction）。"""
    return bool(_CN_CORRECT.search(text or "") or _EN_CORRECT.search(text or ""))


def score_correction(
    text: str, prev_assistant: str | None = None
) -> tuple[bool, float]:
    """A4.2：返回 (是否纠正, 置信度 0~1)。

    置信度组成：
      基础 0.5（命中中/英纠正词即起步）
      + 0.25：紧跟 assistant 回复后的纠正（prev_assistant 非空）
      + 0.15：强否定词命中（不对/别/wrong/...）
      + 0.10：prev_assistant 与 prompt 主题重叠（trigram Jaccard ≥ 0.2）
    """
    if not (_CN_CORRECT.search(text or "") or _EN_CORRECT.search(text or "")):
        return False, 0.0
    score = 0.5
    if prev_assistant:
        score += 0.25
    if _NEGATIVE_HINT.search(text or ""):
        score += 0.15
    if prev_assistant:
        try:
            sim = jaccard(trigrams(prev_assistant), trigrams(text or ""))
            if sim >= 0.2:
                score += 0.1
        except Exception:
            pass
    return True, min(score, 1.0)


def extract_subject(text: str) -> str:
    m = _NP_EXTRACT.search(text or "")
    return m.group(1) if m else (text or "")[:20]


def _clean_inline(text: str, *, limit: int = 180) -> str:
    one = " ".join((text or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1].rstrip() + "…"


def _format_ts(ts: int) -> str:
    if not ts:
        return "unknown time"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _evidence_ref(event: dict[str, Any]) -> str:
    ref = str(event.get("evidence_id") or "")
    if ref:
        return ref[:12]
    ts = event.get("ts", 0)
    sid = str(event.get("session_id") or "")
    return f"{ts}:{sid[:8]}" if sid else str(ts)


def format_evidence(event: dict[str, Any]) -> str:
    """Render review-only evidence from existing event fields.

    This intentionally does not require or mutate the events.ndjson schema.
    """
    tool = event.get("tool") or "agent"
    prompt = _clean_inline(event.get("prompt", ""), limit=160)
    return f"{_format_ts(int(event.get('ts', 0) or 0))} [{tool}] #{_evidence_ref(event)}: {prompt}"


def build_candidate_text(cluster: list[dict[str, Any]], *, kind: str = "correction") -> str:
    """Build a natural-language memory candidate, not structured pattern data."""
    sample = cluster[-1] if cluster else {}
    text = _clean_inline(sample.get("prompt", ""), limit=180)
    if not text:
        return "在本项目中，遵循用户最近反复纠正的工作方式。"

    if _NEGATIVE_HINT.search(text) and _PREFER_HINT.search(text):
        return f"在本项目中，按用户纠正执行：{text}"
    if _NEGATIVE_HINT.search(text):
        return f"在本项目中，避免重复这个被用户纠正的做法：{text}"
    if _PREFER_HINT.search(text):
        return f"在本项目中，优先遵循这个用户偏好：{text}"
    if kind == "preference":
        return f"在本项目中，优先采用这个反复出现的代码偏好：{text}"
    return f"在本项目中，遵循这个用户反复纠正的规则：{text}"


def build_rationale(cluster: list[dict[str, Any]], *, kind: str = "correction") -> str:
    count = len(cluster)
    if kind == "preference":
        return f"该候选来自 {count} 次相似的已接受代码改动；请编辑成自然语言偏好后再保存。"
    has_negative = any(_NEGATIVE_HINT.search(e.get("prompt", "")) for e in cluster)
    has_prefer = any(_PREFER_HINT.search(e.get("prompt", "")) for e in cluster)
    if has_negative and has_prefer:
        return f"用户在 {count} 次相似提示中同时表达了否定做法和替代偏好。"
    if has_negative:
        return f"用户在 {count} 次相似提示中纠正了同类不应采用的做法。"
    if has_prefer:
        return f"用户在 {count} 次相似提示中表达了同类偏好或替代方式。"
    return f"该候选来自 {count} 次相似提示，请审阅后再保存。"


def load_suggestions() -> list[dict[str, Any]]:
    try:
        return json.loads(SUGGESTIONS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_suggestions(items: list[dict[str, Any]]) -> None:
    SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUGGESTIONS_PATH.with_suffix(SUGGESTIONS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.replace(SUGGESTIONS_PATH)


def archive_old(items: list[dict[str, Any]], *, max_active: int) -> list[dict[str, Any]]:
    """超出 max_active 时把最老的归档到 ndjson。"""
    active = [s for s in items if s.get("status") == "pending"]
    if len(active) <= max_active:
        return items
    active.sort(key=lambda s: s.get("created_ts", 0))
    overflow = active[: len(active) - max_active]
    overflow_ids = {s["id"] for s in overflow}
    try:
        SUGGESTIONS_ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SUGGESTIONS_ARCHIVE_PATH.open("a") as f:
            for s in overflow:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return [s for s in items if s["id"] not in overflow_ids]


def run_correction_analyzer(
    correction_events: list[dict[str, Any]],
    *,
    window_seconds: int,
    jaccard_threshold: float,
) -> list[dict[str, Any]]:
    """correction_events: [{ts, project_id, scope, prompt, session_id}]

    返回新生成的 suggestion 列表（每个候选 ≥2 条证据）。
    """
    now = int(time.time())
    fresh = [e for e in correction_events if now - e.get("ts", 0) <= window_seconds]
    if not fresh:
        return []
    # 按 project_id 分组
    by_proj: dict[str, list[dict[str, Any]]] = {}
    for e in fresh:
        by_proj.setdefault(e.get("project_id", ""), []).append(e)

    suggestions: list[dict[str, Any]] = []
    for proj, events in by_proj.items():
        items = [(e.get("prompt", ""), e) for e in events]
        clusters = cluster_by_similarity(items, threshold=jaccard_threshold)
        for cluster in clusters:
            sample = cluster[0]
            text = build_candidate_text(cluster)
            subj = extract_subject(sample.get("prompt", ""))
            scope = sample.get("scope", f"project:{proj}") if proj else "global"
            cluster_conf = min(0.95, 0.4 + 0.1 * len(cluster))
            # A4.2：合入逐事件 is_correction 置信度的均值，避免单条 cluster + 弱信号过度自信
            confs = [
                float(e.get("is_correction_confidence", 0.0) or 0.0)
                for e in cluster
                if "is_correction_confidence" in e
            ]
            if confs:
                conf_final = min(cluster_conf, sum(confs) / len(confs))
            else:
                conf_final = cluster_conf
            suggestions.append(
                {
                    "id": f"sug_{uuid.uuid4().hex[:10]}",
                    "kind": "rule",
                    "scope": scope,
                    "candidate_text": text[:240],
                    "rationale": build_rationale(cluster),
                    "evidence": [format_evidence(e) for e in cluster[:8]],
                    "evidence_event_ids": [_evidence_ref(e) for e in cluster],
                    "confidence": conf_final,
                    "extracted_entities": (
                        [{"canonical": subj, "role": "subject", "patterns": [subj]}] if subj else []
                    ),
                    "created_ts": now,
                    "status": "pending",
                }
            )
    return suggestions


def run_ngram_analyzer(
    post_tool_events: list[dict[str, Any]],
    *,
    window_seconds: int,
    min_occurrences: int,
    min_accept_rate: float,
) -> list[dict[str, Any]]:
    now = int(time.time())
    fresh = [e for e in post_tool_events if now - e.get("ts", 0) <= window_seconds]
    if not fresh:
        return []
    by_proj: dict[str, list[dict[str, Any]]] = {}
    for e in fresh:
        by_proj.setdefault(e.get("project_id", ""), []).append(e)

    suggestions: list[dict[str, Any]] = []
    for proj, events in by_proj.items():
        # A1.3：若 daemon 在 PreToolUse↔PostToolUse 配对时挂载了 intent_summary，
        # 把它拼到 diff_summary 之前作为 n-gram 输入（语义："用户原本要的 + 实际改的"一起聚合）。
        # 仍是 pure 函数：仅按 dict 读字段，不引入外部依赖。
        enriched: list[dict[str, Any]] = []
        for e in events:
            intent = e.get("intent_summary") or ""
            diff = e.get("diff_summary") or ""
            if intent:
                merged = e.copy()
                merged["diff_summary"] = f"{intent} {diff}".strip()
                enriched.append(merged)
            else:
                enriched.append(e)
        ngs = ngram_aggregate(
            enriched,
            min_occurrences=min_occurrences,
            min_accept_rate=min_accept_rate,
        )
        for ng in ngs:
            text = f"在本项目中，优先采用这个反复出现的代码偏好：{ng['ngram']}"
            suggestions.append(
                {
                    "id": f"sug_{uuid.uuid4().hex[:10]}",
                    "kind": "preference",
                    "scope": f"project:{proj}" if proj else "global",
                    "candidate_text": text,
                    "rationale": (
                        f"该候选来自 {ng['count']} 次相似的已接受代码改动，"
                        f"接受率 {ng['accept_rate']:.0%}；请编辑成自然语言偏好后再保存。"
                    ),
                    "evidence": [
                        format_evidence(
                            {
                                "ts": e.get("ts", 0),
                                "tool": e.get("tool", "PostToolUse"),
                                # 用 enriched 的合并文本作 evidence prompt，便于人工 review 看到 intent 上下文
                                "prompt": e.get("diff_summary", ""),
                                "session_id": e.get("session_id", ""),
                                "evidence_id": e.get("evidence_id", ""),
                            }
                        )
                        for e in enriched
                        if ng["ngram"] in " ".join((e.get("diff_summary", "") or "").lower().split())
                    ][:8],
                    "evidence_event_ids": [],
                    "confidence": min(0.95, ng["accept_rate"]),
                    "extracted_entities": [
                        {"canonical": ng["ngram"], "role": "preferred", "patterns": [ng["ngram"]]}
                    ],
                    "created_ts": now,
                    "status": "pending",
                }
            )
    return suggestions


def merge_suggestions(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 candidate_text 去重（仅 status=pending）。"""
    seen = {s.get("candidate_text") for s in existing if s.get("status") == "pending"}
    for s in new:
        if s.get("candidate_text") in seen:
            continue
        existing.append(s)
        seen.add(s.get("candidate_text"))
    return existing
