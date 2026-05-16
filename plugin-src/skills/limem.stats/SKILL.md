---
name: limem.stats
description: Show LiMem local cache statistics and backend ping result.
arguments: []
---

# /limem.stats — 健康 & 统计

调 MCP `limem_stats` + `limem_ping`，渲染：
- 本地 patterns_active / patterns_tombstoned / events_active / events_tombstoned
- 后端 user_name / db_id / db_health
