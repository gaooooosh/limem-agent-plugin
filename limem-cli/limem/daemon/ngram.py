"""N=3 N-gram 词频 + 接受率统计：F3 用。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_TOKEN_RE = re.compile(r"[一-鿿\w]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2]


def ngrams(tokens: list[str], n: int = 3) -> list[tuple[str, ...]]:
    if len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def aggregate(
    events: list[dict[str, Any]],
    *,
    min_occurrences: int = 5,
    min_accept_rate: float = 0.8,
    n: int = 3,
) -> list[dict[str, Any]]:
    """events: [{accepted: bool, diff_summary: str}]；返回满足阈值的 N-gram 列表。"""
    total = Counter()
    accepted = Counter()
    for ev in events:
        text = ev.get("diff_summary", "")
        acc = bool(ev.get("accepted", False))
        for g in ngrams(tokenize(text), n=n):
            total[g] += 1
            if acc:
                accepted[g] += 1
    out: list[dict[str, Any]] = []
    for g, cnt in total.items():
        if cnt < min_occurrences:
            continue
        rate = accepted[g] / cnt if cnt else 0.0
        if rate < min_accept_rate:
            continue
        out.append({"ngram": " ".join(g), "count": cnt, "accept_rate": rate})
    out.sort(key=lambda r: (-r["count"], -r["accept_rate"]))
    return out
