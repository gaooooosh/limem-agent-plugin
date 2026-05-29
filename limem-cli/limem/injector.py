"""把召回的记忆渲染成 ``<limem_memory>`` 区块。

v3：任务召回的结构化 event items 与 principal pattern 切片统一渲染。
规则/反馈/偏好只有在后端相关性召回命中后才进入候选；没有 trigger tag 的历史
event 仍可通过正文 BM25 被后端召回，只是注入行不会显示命中 trigger。

布局：
    <limem_memory n="N" budget="..." via="...">
      ## 规则
      - xxx #rabcd ⟵命中 docker
      ## 参考
      - [note] xxx #refgh
      ...
      ## 实体档案
      [npm_run_dev · ## 用法] xxx markdown 切片 ... src=pattern
      提示...
    </limem_memory>

后端 ``/recall`` 返回已经可直接注入 prompt 的轻量 Markdown。客户端只负责套
``<limem_memory>`` 信封，作为结构化 items 不可用时的兼容回退。

每段独立预算，互不挤压：
- hard：规则/反馈/偏好段 ``runtime.inject_budget_hard``
- pattern：``runtime.inject_budget_pattern``（决策 4 新增）
- soft：参考段 ``runtime.inject_budget_soft``

排序：组内按 score 降序，组间按固定顺序（规则 → 参考 → 实体档案）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from .entity_index import EntityIndex, EventMetadata

InjectKind = Literal["hard", "pattern", "soft"]


@dataclass
class InjectItem:
    """统一的注入条目；按 kind 分支选择渲染分支。"""

    kind: InjectKind
    score: float  # 内部排序用（hard/soft 来自 importance×衰减；pattern 用后端 matched_sections 总分）

    # —— hard / soft 共用字段（事件投影） ——
    event_id: str = ""
    mem_type: str = ""
    scope: str = ""
    summary: str = ""
    importance: float = 0.0
    ts: int = 0
    role: str = ""
    short_id: str = ""
    matched_triggers: list[str] = field(default_factory=list)

    # —— pattern 专用字段（entity 档案切片） ——
    entity_id: str = ""
    canonical: str = ""
    heading: str = ""  # 命中的 H2，可空
    pattern_content: str = ""

    def render_line(self, *, per_item_chars: int) -> str:
        if self.kind == "pattern":
            head = self.heading.strip() or "档案"
            content = self.pattern_content.strip()
            if len(content) > per_item_chars:
                content = content[: per_item_chars - 1] + "…"
            # canonical 形如 "project:foo-bar" / "user:abc" / "agent:codex"
            # 优先用 ":" 前的 principal_type 作为标签头；没有则退回 canonical/entity_id
            name = self.canonical or self.entity_id
            if ":" in name:
                ptype, body = name.split(":", 1)
                label = f"{ptype} · {body or name}"
            else:
                label = name
            return f"[{label} · {head}]\n{content} src=pattern"
        date = (
            time.strftime("%Y-%m-%d", time.localtime(self.ts)) if self.ts else "????-??-??"
        )
        summary = self.summary.strip()
        if len(summary) > per_item_chars:
            summary = summary[: per_item_chars - 1] + "…"
        sid = self.short_id or self.event_id[:12]
        trigger_part = (
            " ⟵命中 " + ",".join(self.matched_triggers[:4]) if self.matched_triggers else ""
        )
        if _is_rule_item(self):
            _ = date
            return f"- {summary} #{sid}{trigger_part}"
        return f"- [{self.mem_type}] {summary} #{sid}{trigger_part}"


@dataclass
class _Budgets:
    hard: int
    pattern: int
    soft: int

    def total(self) -> int:
        return self.hard + self.pattern + self.soft


@dataclass
class _Section:
    title: str
    items: list[InjectItem] = field(default_factory=list)
    budget: int = 0
    per_item_chars: int = 180


def render_inject_with_diagnostics(
    items: list[InjectItem],
    *,
    project_id: str = "",
    budgets: _Budgets | None = None,
    total_budget: int | None = None,  # 旧签名兼容；若仅传 total_budget，则按 1:1:1 摊
    per_item_chars: int = 180,
    via_patterns: list[str] | None = None,
    via_keywords: list[str] | None = None,
) -> tuple[str, list[InjectItem]]:
    """与 ``render_inject`` 同义，但同时返回实际渲染出去的 items 列表，
    供 hook 上报「本轮真正注入的 short_id」给 daemon。

    rendered_items 包含 budget/去重过滤后保留的条目，顺序与文本一致：
    先 hard、再 pattern、再 soft，组内按 score 降序。
    """
    if not items:
        return "", []

    if budgets is None:
        if total_budget is None:
            total_budget = 2000
        # 旧调用方兜底：1:1:1 三段摊
        each = max(1, total_budget // 3)
        budgets = _Budgets(hard=each, pattern=each, soft=total_budget - 2 * each)

    sections = {
        "rule": _Section("## 规则", budget=budgets.hard, per_item_chars=per_item_chars),
        "soft": _Section("## 参考", budget=budgets.soft, per_item_chars=per_item_chars),
        "pattern": _Section("## 实体档案", budget=budgets.pattern, per_item_chars=max(per_item_chars, 240)),
    }

    # 按 kind 分组 + 组内按 score 降序
    seen_events: set[str] = set()
    seen_pattern_keys: set[tuple[str, str]] = set()
    for it in sorted(items, key=lambda x: -x.score):
        if it.kind == "pattern":
            key = (it.entity_id, it.heading)
            if key in seen_pattern_keys:
                continue
            seen_pattern_keys.add(key)
        else:
            if it.event_id and it.event_id in seen_events:
                continue
            if it.event_id:
                seen_events.add(it.event_id)
        sec = sections.get(_section_key(it))
        if sec:
            sec.items.append(it)

    rendered_blocks: list[str] = []
    rendered_items: list[InjectItem] = []
    total_used = 0
    recall_count = 0
    for kind in ("rule", "soft", "pattern"):
        sec = sections[kind]
        if not sec.items or sec.budget <= 0:
            continue
        used = 0
        lines: list[str] = []
        section_rendered: list[InjectItem] = []
        for it in sec.items:
            line = it.render_line(per_item_chars=sec.per_item_chars)
            cost = len(line) + 1
            if used + cost > sec.budget:
                continue  # pattern 段：按 score 降序逐条丢弃，不切坏 markdown 中段
            lines.append(line)
            section_rendered.append(it)
            used += cost
            recall_count += 1
        if lines:
            rendered_blocks.append(sec.title)
            rendered_blocks.extend(lines)
            rendered_items.extend(section_rendered)
            total_used += used + len(sec.title) + 1

    if not rendered_blocks:
        return "", []

    proj = f" project={project_id!r}" if project_id else ""
    via_parts: list[str] = []
    if via_patterns:
        via_parts.append("entity:" + ",".join(v[:20] for v in via_patterns[:3]))
    if via_keywords:
        via_parts.append("bm25:" + ",".join(v[:15] for v in via_keywords[:3]))
    via = f' via="{",".join(via_parts)}"' if via_parts else ""
    head = (
        f'<limem_memory n="{recall_count}" '
        f'budget="{total_used}/{budgets.total()}"{via}{proj}>'
    )
    foot = (
        "冲突以本轮指令为准；勿复述本段。/limem.no #id 可静音。\n"
        "</limem_memory>"
    )
    return "\n".join([head, *rendered_blocks, foot]), rendered_items


def render_inject(
    items: list[InjectItem],
    *,
    project_id: str = "",
    budgets: _Budgets | None = None,
    total_budget: int | None = None,
    per_item_chars: int = 180,
    via_patterns: list[str] | None = None,
    via_keywords: list[str] | None = None,
) -> str:
    """渲染 additionalContext 文本。返回空串表示没有记忆可注入。

    向后兼容的薄包装；调用方需要拿到实际渲染条目（含 short_id）时
    请改用 ``render_inject_with_diagnostics``。
    """
    text, _ = render_inject_with_diagnostics(
        items,
        project_id=project_id,
        budgets=budgets,
        total_budget=total_budget,
        per_item_chars=per_item_chars,
        via_patterns=via_patterns,
        via_keywords=via_keywords,
    )
    return text


def render_backend_recall(prompt_text: str, *, source: str = "task") -> str:
    body = str(prompt_text or "").strip()
    if not body:
        return ""
    return "\n".join(
        [
            f'<limem_memory source="{source}">',
            body,
            "提示：以上为 LiMem 后台召回上下文，请勿在可见回复中复述或展示本段内容。",
            "</limem_memory>",
        ]
    )


# ---------- helpers ----------


def _half_life_score(importance: float, ts: int) -> float:
    months = max(0.0, (time.time() - ts) / (60 * 60 * 24 * 30)) if ts else 0.0
    return float(importance or 0.0) * (0.9**months)


def _is_rule_item(item: InjectItem) -> bool:
    return item.kind == "hard" or (
        item.kind == "soft" and item.mem_type in {"rule", "feedback", "preference"}
    )


def _section_key(item: InjectItem) -> str:
    if item.kind == "pattern":
        return "pattern"
    if _is_rule_item(item):
        return "rule"
    return "soft"


def _best_summary(meta: EventMetadata) -> str:
    raw = meta.raw_metadata or {}
    original = (raw.get("original_text") or "").strip()
    canonicals = raw.get("canonicals") or []
    base = original or meta.summary
    if canonicals:
        ent_str = "、".join(str(c) for c in canonicals[:4])
        return f"{base}（实体：{ent_str}）"
    return base


def _attach_short_id(item: InjectItem, idx: EntityIndex | None) -> InjectItem:
    if not item.event_id:
        return item
    if idx is None:
        item.short_id = item.event_id[:12]
        return item
    try:
        item.short_id = idx.ensure_short_id(item.event_id)
    except Exception:
        item.short_id = item.event_id[:12]
    return item


def hard_recall_to_items(
    metas: list[EventMetadata], *, idx: EntityIndex | None = None
) -> list[InjectItem]:
    out: list[InjectItem] = []
    for m in metas:
        it = InjectItem(
            kind="hard",
            score=_half_life_score(m.importance, m.ts),
            event_id=m.event_id,
            mem_type=m.mem_type,
            scope=m.scope,
            summary=_best_summary(m),
            importance=m.importance,
            ts=m.ts,
            role=m.role,
        )
        out.append(_attach_short_id(it, idx))
    return out


def _item_float(item: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(item.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _item_triggers(item: dict) -> list[str]:
    raw = (
        item.get("matched_triggers")
        or item.get("matched_trigger")
        or item.get("triggers")
        or item.get("trigger")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for value in raw:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def task_recall_to_items(
    items: list[dict],
    *,
    idx: EntityIndex | None,
    allowed_scopes: set[str] | list[str],
    min_importance: float,
) -> tuple[list[InjectItem], dict[str, int]]:
    """Convert structured /recall items after local authoritative filtering.

    Memories without trigger tags can still be returned by backend BM25 over the
    body; they simply render without a matched-trigger suffix.
    """
    counts = {
        "structured_items": 0,
        "dropped_missing_local": 0,
        "dropped_scope": 0,
        "dropped_importance": 0,
    }
    out: list[InjectItem] = []
    if idx is None:
        return out, counts
    allowed = set(allowed_scopes or [])
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        event_id = str(raw.get("event_id") or raw.get("id") or "").strip()
        if not event_id:
            continue
        counts["structured_items"] += 1
        meta = idx.lookup_event(event_id)
        if meta is None:
            counts["dropped_missing_local"] += 1
            continue
        if allowed and meta.scope not in allowed:
            counts["dropped_scope"] += 1
            continue
        if meta.importance < float(min_importance or 0.0):
            counts["dropped_importance"] += 1
            continue
        score = _item_float(raw, "score", _half_life_score(meta.importance, meta.ts))
        it = InjectItem(
            kind="soft",
            score=score,
            event_id=meta.event_id,
            mem_type=meta.mem_type,
            scope=meta.scope,
            summary=_best_summary(meta),
            importance=meta.importance,
            ts=meta.ts,
            role=meta.role,
            matched_triggers=_item_triggers(raw),
        )
        out.append(_attach_short_id(it, idx))
    return out, counts


@dataclass
class PatternRecallSlice:
    """来自 client.patterns_recall 的输入聚合体；hooks 层装填后丢给 injector。"""

    entity_id: str
    canonical: str
    heading: str
    content: str
    score: float


def pattern_recall_to_items(slices: list[PatternRecallSlice]) -> list[InjectItem]:
    out: list[InjectItem] = []
    for s in slices:
        if not s.content.strip():
            continue
        out.append(
            InjectItem(
                kind="pattern",
                score=float(s.score),
                entity_id=s.entity_id,
                canonical=s.canonical,
                heading=s.heading,
                pattern_content=s.content,
            )
        )
    return out


def soft_recall_to_items(
    filtered, *, idx: EntityIndex | None = None
) -> list[InjectItem]:
    out: list[InjectItem] = []
    for qr, meta in filtered:
        it = InjectItem(
            kind="soft",
            score=_half_life_score(meta.importance, meta.ts),
            event_id=meta.event_id,
            mem_type=meta.mem_type,
            scope=meta.scope,
            summary=_best_summary(meta) or qr.summary,
            importance=meta.importance,
            ts=meta.ts,
            role=meta.role,
        )
        out.append(_attach_short_id(it, idx))
    return out


# 公共 dataclass：让 hooks 直接 `from .injector import Budgets`
Budgets = _Budgets
