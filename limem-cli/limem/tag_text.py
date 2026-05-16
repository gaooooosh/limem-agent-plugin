"""tag-as-token：把 metadata 序列化为 BM25 可索引的 token 字符串。

格式约定：
- ``[limem.<key>=<value>]`` — 单值（如 scope/type）
- ``[limem.<key>= <a> | <b> | <c>]`` — 列表（如 patterns/canonical）

设计动机：LiMem 后端 query 端点不接 filters，scope/type 过滤必须靠 BM25 命中 + 客户端二次过滤。
"""

from __future__ import annotations

import re
from typing import Iterable

_TAG_RE = re.compile(r"\[limem\.([a-z_]+)=([^\]]*)\]")


def encode_tags(**kwargs: str | Iterable[str] | None) -> str:
    """把扁平 kv 转成 token 串。值为列表时用 ``|`` 分隔，前后各加空格确保 BM25 分词。"""
    parts: list[str] = []
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(f"[limem.{key}={value}]")
        else:
            items = list(value)
            if not items:
                continue
            joined = " | ".join(items)
            parts.append(f"[limem.{key}= {joined} ]")
    return " ".join(parts)


def extract_tags(text: str) -> dict[str, list[str]]:
    """从 BM25 命中的 summary/action 文本里抽 ``[limem.x=...]`` token，用于二次过滤。"""
    out: dict[str, list[str]] = {}
    for m in _TAG_RE.finditer(text or ""):
        key = m.group(1)
        raw = m.group(2).strip()
        if " | " in raw:
            values = [v.strip() for v in raw.split("|") if v.strip()]
        else:
            values = [raw] if raw else []
        out.setdefault(key, []).extend(values)
    return out


def build_recall_query(
    user_prompt: str,
    *,
    scopes: list[str],
    types: list[str],
    canonical_hints: list[str] | None = None,
) -> str:
    """召回查询：把允许的 scope/type 也 token 化，让 BM25 同时利用 prompt 和 tag。"""
    tag = encode_tags(
        scope=scopes,
        type=types,
        canonical=canonical_hints or [],
    )
    return f"{tag} {user_prompt}".strip()


def matches_scope(text: str, allowed_scopes: set[str]) -> bool:
    """二次过滤：仅当 text 含至少一个 allowed scope token 时通过。"""
    tags = extract_tags(text)
    found = set(tags.get("scope", []))
    return bool(found & allowed_scopes)


def filter_by_scope_and_type(
    items: list[tuple[str, dict]],
    *,
    allowed_scopes: set[str],
    allowed_types: set[str] | None = None,
    excluded_types: set[str] | None = None,
) -> list[tuple[str, dict]]:
    """通用过滤：``items`` 形如 [(text_for_tag_extract, raw_record)]，返回保留项。"""
    out: list[tuple[str, dict]] = []
    for text, raw in items:
        tags = extract_tags(text)
        scopes = set(tags.get("scope", []))
        types = set(tags.get("type", []))
        if allowed_scopes and not (scopes & allowed_scopes):
            continue
        if allowed_types and types and not (types & allowed_types):
            continue
        if excluded_types and (types & excluded_types):
            continue
        out.append((text, raw))
    return out
