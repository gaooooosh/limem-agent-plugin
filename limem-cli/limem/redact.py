"""隐私正则黑名单，写入前拦截。"""

from __future__ import annotations

import re


def contains_secret(text: str, patterns: list[str]) -> str | None:
    """返回首个命中的子串；都没命中返回 None。"""
    for p in patterns:
        try:
            m = re.search(p, text)
        except re.error:
            continue
        if m:
            return m.group(0)
    return None
