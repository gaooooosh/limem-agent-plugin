"""把召回的记忆渲染成 ``<limem_memory>`` 区块。

阶段 4：head 加 via=（pattern + bm25 关键词）；每行末尾加 ``#<short_id> src=<source>``。
预算守恒：``per_item_chars`` 默认 200 → 180（挤 20 字给 short_id+src 后缀）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .pattern_index import EventMetadata, PatternHit, PatternIndex


@dataclass
class InjectItem:
    event_id: str
    mem_type: str
    scope: str
    summary: str
    importance: float
    ts: int
    source: str = "hard"  # hard | soft | pattern
    role: str = ""
    short_id: str = ""

    def score(self) -> float:
        months = max(0.0, (time.time() - self.ts) / (60 * 60 * 24 * 30)) if self.ts else 0.0
        return float(self.importance) * (0.9**months)


def render_inject(
    items: list[InjectItem],
    *,
    project_id: str = "",
    total_budget: int = 2000,
    per_item_chars: int = 180,
    via_patterns: list[str] | None = None,
    via_keywords: list[str] | None = None,
) -> str:
    """渲染 additionalContext 文本。返回空串表示没有记忆可注入。"""
    if not items:
        return ""

    src_rank = {"pattern": 0, "hard": 1, "soft": 2}
    items_sorted = sorted(
        items, key=lambda it: (src_rank.get(it.source, 9), -it.score())
    )

    seen: set[str] = set()
    deduped: list[InjectItem] = []
    for it in items_sorted:
        if it.event_id and it.event_id in seen:
            continue
        if it.event_id:
            seen.add(it.event_id)
        deduped.append(it)

    rendered: list[str] = []
    used = 0
    for it in deduped:
        summary = it.summary.strip()
        if len(summary) > per_item_chars:
            summary = summary[: per_item_chars - 1] + "…"
        date = (
            time.strftime("%Y-%m-%d", time.localtime(it.ts)) if it.ts else "????-??-??"
        )
        role_part = f" · {it.role}" if it.role else ""
        sid = it.short_id or it.event_id[:12]
        line = (
            f"[{it.mem_type} · {it.scope} · {date}{role_part}]\n"
            f"{summary} #{sid} src={it.source}"
        )
        if used + len(line) + 1 > total_budget:
            break
        rendered.append(line)
        used += len(line) + 1

    if not rendered:
        return ""

    proj = f" project={project_id!r}" if project_id else ""
    via_parts: list[str] = []
    if via_patterns:
        via_parts.append("pattern:" + " | ".join(v[:20] for v in via_patterns[:3]))
    if via_keywords:
        via_parts.append("bm25:" + " ".join(v[:15] for v in via_keywords[:2]))
    via = f' via="{" | ".join(via_parts)}"' if via_parts else ""
    head = (
        f'<limem_memory recall="{len(rendered)}" '
        f'budget="{used}/{total_budget}"{via}{proj}>'
    )
    foot = (
        "提示：以上为 LiMem 召回的长期记忆。冲突以本轮指令为准；可用 "
        "`/limem.fix #<id> <新文本>` 修订，或 `/limem.no #<id>` 本会话静音。\n"
        "</limem_memory>"
    )
    return "\n".join([head, *rendered, foot])


def _best_summary(meta: EventMetadata) -> str:
    raw = meta.raw_metadata or {}
    original = (raw.get("original_text") or "").strip()
    canonicals = raw.get("canonicals") or []
    base = original or meta.summary
    if canonicals:
        ent_str = "、".join(str(c) for c in canonicals[:4])
        return f"{base}（实体：{ent_str}）"
    return base


def _attach_short_id(item: InjectItem, pidx: PatternIndex | None) -> InjectItem:
    if not item.event_id:
        return item
    if pidx is None:
        item.short_id = item.event_id[:12]
        return item
    try:
        item.short_id = pidx.ensure_short_id(item.event_id)
    except Exception:
        item.short_id = item.event_id[:12]
    return item


def hard_recall_to_items(
    metas: list[EventMetadata], *, pidx: PatternIndex | None = None
) -> list[InjectItem]:
    out: list[InjectItem] = []
    for m in metas:
        it = InjectItem(
            event_id=m.event_id,
            mem_type=m.mem_type,
            scope=m.scope,
            summary=_best_summary(m),
            importance=m.importance,
            ts=m.ts,
            source="hard",
            role=m.role,
        )
        out.append(_attach_short_id(it, pidx))
    return out


def pattern_recall_to_items(
    hits: list[PatternHit],
    metadata_lookup,
    *,
    pidx: PatternIndex | None = None,
) -> list[InjectItem]:
    out: list[InjectItem] = []
    for h in hits:
        if not h.event_id:
            continue
        meta = metadata_lookup(h.event_id)
        if meta is None:
            continue
        it = InjectItem(
            event_id=meta.event_id,
            mem_type=meta.mem_type,
            scope=meta.scope,
            summary=_best_summary(meta),
            importance=meta.importance,
            ts=meta.ts,
            source="pattern",
            role=meta.role,
        )
        out.append(_attach_short_id(it, pidx))
    return out


def soft_recall_to_items(
    filtered, *, pidx: PatternIndex | None = None
) -> list[InjectItem]:
    out: list[InjectItem] = []
    for qr, meta in filtered:
        it = InjectItem(
            event_id=meta.event_id,
            mem_type=meta.mem_type,
            scope=meta.scope,
            summary=_best_summary(meta) or qr.summary,
            importance=meta.importance,
            ts=meta.ts,
            source="soft",
            role=meta.role,
        )
        out.append(_attach_short_id(it, pidx))
    return out
