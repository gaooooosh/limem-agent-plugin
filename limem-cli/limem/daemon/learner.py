"""被动学习器：周期性扫 events.ndjson 生成 suggestions。

F2 (correction 聚合): 中英纠正关键词 → trigram Jaccard → 候选 rule
F3 (N-gram preference): PostToolUse 接受率 → 候选 preference
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from ..config import (
    SUGGESTIONS_ARCHIVE_PATH,
    SUGGESTIONS_PATH,
)
from .jaccard import cluster_by_similarity
from .ngram import aggregate as ngram_aggregate

_CN_CORRECT = re.compile(r"不对|应该|别|不要|改成|纠正|换成|不是")
_EN_CORRECT = re.compile(r"don'?t|actually|stop|wrong|instead|prefer", re.IGNORECASE)
_NP_EXTRACT = re.compile(r"([一-鿿\w]{2,8})(?=\s*(?:不对|要|改成|换成|换))")


def is_correction(text: str) -> bool:
    return bool(_CN_CORRECT.search(text or "") or _EN_CORRECT.search(text or ""))


def extract_subject(text: str) -> str:
    m = _NP_EXTRACT.search(text or "")
    return m.group(1) if m else (text or "")[:20]


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
            text = sample.get("prompt", "")
            subj = extract_subject(text)
            scope = sample.get("scope", f"project:{proj}") if proj else "global"
            suggestions.append(
                {
                    "id": f"sug_{uuid.uuid4().hex[:10]}",
                    "kind": "rule",
                    "scope": scope,
                    "candidate_text": text[:200],
                    "evidence_event_ids": [e.get("ts") for e in cluster],
                    "confidence": min(0.95, 0.4 + 0.1 * len(cluster)),
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
        ngs = ngram_aggregate(
            events,
            min_occurrences=min_occurrences,
            min_accept_rate=min_accept_rate,
        )
        for ng in ngs:
            text = f"prefer pattern: {ng['ngram']}"
            suggestions.append(
                {
                    "id": f"sug_{uuid.uuid4().hex[:10]}",
                    "kind": "preference",
                    "scope": f"project:{proj}" if proj else "global",
                    "candidate_text": text,
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
