"""limemd — LiMem 后台 daemon。

职责：
- 通过 unix-socket JSON-RPC 提供 read/write API
- 异步消费 events.ndjson 事件总线
- 独占 SQLite 写句柄
- 维护连通性状态机、暂停状态、本地缓存
- 周期性运行 learner（被动学习）
"""

from __future__ import annotations
