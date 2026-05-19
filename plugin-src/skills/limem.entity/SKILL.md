---
name: limem.entity
description: >-
  Manage LiMem principals — the entities that carry markdown profiles in v3.
  Default principals (user / agent / project) are auto-ensured on SessionStart;
  use this skill to register optional principals (team / service), list active
  principals, or activate / deactivate one. Calls MCP tools
  limem_principal_list / register / activate / deactivate.
arguments: [subcommand, ...]
---

# /limem.entity — Principal 管理

## 何时调用

- 用户问 "我有哪些 principal" / "show me active principals"
- 用户想注册团队 / 服务级 principal（如 `team:platform` / `service:auth`）以共享档案
- 用户想暂停 / 重新启用某个 principal 在 recall 中的参与

## 子命令

### `list`

调 `limem_principal_list(active_only=true)`，渲染：

```
📛 Active Principals（N）

  • [project] principal_project_<sha8>  canonical=<project_id>  pattern=✓
  • [user]    principal_user_<sha8>     canonical=user:<user_id>  pattern=·
  • [agent]   principal_agent_codex     canonical=agent:codex   pattern=✓
```

`pattern=✓` 表示后端已挂 markdown；`·` 表示未挂。

### `register`

参数：`type slug [description] [alias1,alias2,...]`

- `type` ∈ `team` / `service`（默认 principal `user/agent/project` 不需要手动注册）
- `slug`：唯一短名，例如 `platform` / `payments-svc`
- `description`：人类可读说明
- `aliases`：英文 / 中文 / 缩写等，BM25 + entity 检索能命中

调 `limem_principal_register`，回执：
```
✓ registered <entity_id>（principal_team_<slug>）
   下次 SessionStart 与匹配 prompt 时它的档案会参与并发 recall。
   下一步：/limem.pattern <entity_id> put 给它挂上档案。
```

### `activate <alias_or_id>`

调 `limem_principal_activate`。重新激活的 principal 会重新参与 recall。

### `deactivate <alias_or_id>`

调 `limem_principal_deactivate`。被停用的 principal 不再参与 recall，但 markdown
档案保留；可随时 `activate` 恢复。

## 注意

- 默认 principal（user / agent / project）由 SessionStart hook lazy ensure，
  通常无需手动 register。
- principal 是稀缺资源（典型 3 ~ 8 个），不要把所有 mention 都升级成 principal。
- 删除 principal 本身的命令在 `limem entity prune-legacy` CLI 调试入口；本 skill
  不提供 delete，避免误操作。
