"""DEPRECATED 兼容层：旧名指向新的 entity_index。

v2 起 pattern 数据由后端整篇 markdown 持有，本地不再维护 trigger 短语 trigram 索引。
本文件保留是为了让仍未迁移的导入路径（如旧测试、外部脚本）不直接报错；新代码请：

    from limem.entity_index import EntityIndex, EntityHit, EventMetadata

旧的 ``PatternIndex.search_patterns`` 已无对应实现——调用方应迁到
``EntityIndex.search_entities`` + ``LimemClient.patterns_recall``。
"""

from __future__ import annotations

from .entity_index import (  # noqa: F401
    SCHEMA_VERSION,
    EntityHit,
    EntityIndex,
    EventMetadata,
)

# 旧名兼容（class 标识符级别 alias）
PatternIndex = EntityIndex

__all__ = [
    "EntityHit",
    "EntityIndex",
    "EventMetadata",
    "PatternIndex",
    "SCHEMA_VERSION",
]
