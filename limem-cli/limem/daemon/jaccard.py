"""Trigram Jaccard 相似度：F2 同主题聚合用。"""

from __future__ import annotations


def trigrams(text: str) -> set[str]:
    """3 字符滑动窗口；中英文皆适用。"""
    s = (text or "").lower().strip()
    if len(s) < 3:
        return {s} if s else set()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def cluster_by_similarity(
    items: list[tuple[str, dict]],  # [(text, payload)]
    *,
    threshold: float = 0.4,
) -> list[list[dict]]:
    """简单单链聚类：与已有簇任一成员相似度 >= threshold 即并入。"""
    clusters: list[dict] = []  # [{"trigrams": set, "members": [payload, ...]}]
    for text, payload in items:
        tg = trigrams(text)
        if not tg:
            continue
        placed = False
        for cl in clusters:
            for member_tg in cl["trigrams_list"]:
                if jaccard(tg, member_tg) >= threshold:
                    cl["members"].append(payload)
                    cl["trigrams_list"].append(tg)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append({"trigrams_list": [tg], "members": [payload]})
    return [cl["members"] for cl in clusters if len(cl["members"]) >= 2]
