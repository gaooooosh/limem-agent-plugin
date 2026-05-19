"""Principal 主体注册与解析（v3 引入）。

v3 模型：pattern markdown 只挂在少量 principal 主体上，不再为每个 mention 注册 entity。

默认 principal：
- ``user``    — 当前 LiMem 账号用户
- ``agent``   — 正在协作的 AI Agent（``claude-code`` / ``codex``）
- ``project`` — 当前工作的项目（按 scope.detect_project_id 解析）

可选 principal：``team`` / ``service``（用户手动注册）。

稳定 ID 规则：

    principal_user_<sha8(user_id)>
    principal_agent_<slug(tool)>
    principal_project_<sha8(project_id)>
    principal_team_<slug>
    principal_service_<slug>

注册后行为：
- 后端 ``entity_create_or_promote`` 写入 entity_type=``principal``，并把 aliases /
  description / metadata 同步到 ``EntityIndex.upsert_principal`` 本地镜像。
- 已存在则用 ``entity_patch`` 增量更新 aliases / description；失败静默（hook 不能阻塞）。
- 调用方可通过 ``principal_alias_to_id("project" | "user" | "agent", ...)`` 在 CLI/MCP
  层把易记别名解析为 stable entity_id。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .client import LimemClient
    from .config import Credentials
    from .entity_index import EntityIndex


PrincipalType = Literal["user", "agent", "project", "team", "service"]


# ---------- 基础工具 ----------


def sha8(value: str) -> str:
    """稳定 8 位 sha1 前缀；空值返回 ``00000000``。"""
    if not value:
        return "00000000"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def slugify(value: str, *, max_len: int = 32) -> str:
    """转小写、非字母数字下划线变 ``_``、合并连续下划线、截断。"""
    s = re.sub(r"[^\w]+", "_", (value or "").lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "x")


# ---------- Spec ----------


@dataclass
class PrincipalSpec:
    """注册 principal 所需的全部字段。"""

    principal_type: PrincipalType
    slug: str
    description: str
    aliases: list[str] = field(default_factory=list)
    scope: str = "global"
    tool: str = ""           # 仅 agent principal 有意义
    project_id: str = ""     # 仅 project principal 有意义
    canonical: str = ""      # 留空则用 slug

    def normalized_canonical(self) -> str:
        return (self.canonical or self.slug).strip() or self.slug


def entity_id_for(spec: PrincipalSpec) -> str:
    """根据 spec 计算稳定 entity_id。"""
    t = spec.principal_type
    if t == "user":
        return f"principal_user_{sha8(spec.slug)}"
    if t == "agent":
        return f"principal_agent_{slugify(spec.slug or spec.tool or 'unknown')}"
    if t == "project":
        return f"principal_project_{sha8(spec.slug)}"
    return f"principal_{t}_{slugify(spec.slug)}"


# ---------- 默认 principal 生成 ----------


def _project_basename(project_id: str) -> str:
    if not project_id:
        return ""
    # project_id 可能形如 github.com/owner/repo 或 cwd-basename-<sha8>
    tail = project_id.rsplit("/", 1)[-1]
    # 去掉末尾的 -<8 位 hex>
    return re.sub(r"-[0-9a-f]{8}$", "", tail)


def default_principals(
    creds: Credentials | None,
    project_id: str,
    tool: str,
) -> list[PrincipalSpec]:
    """生成当前会话应该自动 ensure 的默认 principals。

    - 缺 ``user_id`` 时跳过 user（避免污染：sha8("") = 00000000）
    - ``project_id`` 缺则跳过 project
    - ``tool`` 缺则跳过 agent
    """
    out: list[PrincipalSpec] = []
    user_id = (creds.user_id if creds else "") or ""
    if user_id:
        out.append(
            PrincipalSpec(
                principal_type="user",
                slug=user_id,
                description=f"当前 LiMem 账号用户：{user_id}",
                aliases=["我", "用户", "the user", "myself", user_id],
                scope="global",
                canonical=f"user:{user_id}",
            )
        )

    if tool:
        out.append(
            PrincipalSpec(
                principal_type="agent",
                slug=tool,
                tool=tool,
                description=f"正在协作的 AI Agent：{tool}",
                aliases=["你", "agent", "助手", "assistant", tool],
                scope="global",
                canonical=f"agent:{tool}",
            )
        )

    if project_id:
        basename = _project_basename(project_id) or project_id
        aliases = list({project_id, basename, "this project", "本项目", "当前项目"})
        out.append(
            PrincipalSpec(
                principal_type="project",
                slug=project_id,
                project_id=project_id,
                description=f"当前工作的项目：{project_id}",
                aliases=aliases,
                scope=f"project:{project_id}",
                canonical=f"project:{basename or project_id}",
            )
        )
    return out


# ---------- alias → entity_id 解析 ----------


def principal_alias_to_id(
    alias: str,
    *,
    creds: Credentials | None,
    project_id: str,
    tool: str,
    idx: EntityIndex | None = None,
) -> str:
    """把 ``"project" / "user" / "agent"`` 等易记别名解析为 stable entity_id。

    其它输入：
    - 若 alias 已是 ``principal_*`` 前缀的 entity_id，原样返回
    - 若 idx 提供且能在本地 principals 表找到匹配 alias，返回对应 entity_id
    - 否则原样返回（让上层报错）
    """
    a = (alias or "").strip()
    if not a:
        return ""

    if a.startswith("principal_"):
        return a

    lower = a.lower()
    user_id = (creds.user_id if creds else "") or ""
    if lower == "user" and user_id:
        return entity_id_for(PrincipalSpec(principal_type="user", slug=user_id, description=""))
    if lower == "agent" and tool:
        return entity_id_for(
            PrincipalSpec(principal_type="agent", slug=tool, tool=tool, description="")
        )
    if lower == "project" and project_id:
        return entity_id_for(
            PrincipalSpec(
                principal_type="project", slug=project_id, project_id=project_id, description=""
            )
        )

    # 退路：在本地 principals 表里按 slug / canonical / alias 反查
    if idx is not None:
        try:
            rows = idx.list_principals(active_only=False)
        except Exception:
            rows = []
        for row in rows:
            if row.entity_id == a:
                return row.entity_id
            if row.slug == a or row.canonical == a:
                return row.entity_id
            if a in (row.aliases or []):
                return row.entity_id
    return a


# ---------- 注册与 ensure ----------


def register_principal(
    spec: PrincipalSpec,
    *,
    creds: Credentials | None,
    idx: EntityIndex,
    client: LimemClient | None = None,
    swallow: bool = True,
) -> str:
    """注册 principal 到后端与本地镜像。

    返回 stable entity_id。后端失败默认 swallow（不阻塞 hook）；显式传 ``swallow=False``
    则把异常抛出（CLI 直接命令使用）。
    """
    import time

    from .client import LimemClient as _LimemClient
    from .client import LimemError

    eid = entity_id_for(spec)
    canonical = spec.normalized_canonical()
    aliases = list({a for a in (spec.aliases or []) if a})

    if client is None and creds is not None and creds.api_key and creds.db_id:
        client = _LimemClient(creds=creds, timeout=2.0)

    backend_ok = False
    if client is not None:
        try:
            client.entity_create_or_promote(
                eid,
                spec.description,
                entity_type="principal",
                aliases=aliases,
                metadata={
                    "limem_scope": spec.scope,
                    "principal_type": spec.principal_type,
                    "slug": spec.slug,
                    "tool": spec.tool,
                    "project_id": spec.project_id,
                },
            )
            backend_ok = True
        except LimemError as e:
            if e.status == 409:
                try:
                    client.entity_patch(
                        eid,
                        description=spec.description,
                        add_aliases=aliases,
                        metadata={
                            "limem_scope": spec.scope,
                            "principal_type": spec.principal_type,
                            "slug": spec.slug,
                            "tool": spec.tool,
                            "project_id": spec.project_id,
                        },
                    )
                    backend_ok = True
                except LimemError:
                    if not swallow:
                        raise
            elif not swallow:
                raise
        except Exception:
            if not swallow:
                raise

    try:
        idx.upsert_principal(
            entity_id=eid,
            principal_type=spec.principal_type,
            slug=spec.slug,
            canonical=canonical,
            aliases=aliases,
            description=spec.description,
            scope=spec.scope,
            tool=spec.tool,
            project_id=spec.project_id,
            active=True,
            last_seen_ts=int(time.time()),
            raw_metadata={"backend_ok": backend_ok},
        )
    except Exception:
        if not swallow:
            raise
    return eid


# Ensure dispatcher used by hooks / writer / CLI ---------------------------

_ENSURE_MARKER_DIR = Path("~/.cache/limem/principals_ensured").expanduser()


def _ensured_marker(eid: str) -> Path:
    return _ENSURE_MARKER_DIR / eid


def ensure_default_principals(
    creds: Credentials | None,
    *,
    project_id: str,
    tool: str,
    idx: EntityIndex,
    client: LimemClient | None = None,
    force: bool = False,
) -> list[str]:
    """幂等地确保默认 principals 在后端与本地都注册过。

    第一次成功后会落一个 mtime 文件（``~/.cache/limem/principals_ensured/<eid>``）做
    粗粒度去重；hook 多次触发也只重试本地缺失的项。后端失败永远 swallow。
    """
    out: list[str] = []
    specs = default_principals(creds, project_id, tool)
    if not specs:
        return out

    try:
        _ENSURE_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    for spec in specs:
        eid = entity_id_for(spec)
        marker = _ensured_marker(eid)
        local_hit = None
        try:
            local_hit = idx.lookup_principal(eid)
        except Exception:
            local_hit = None
        if not force and local_hit is not None and marker.exists():
            out.append(eid)
            continue
        register_principal(spec, creds=creds, idx=idx, client=client, swallow=True)
        try:
            marker.touch(exist_ok=True)
        except Exception:
            pass
        out.append(eid)
    return out


__all__ = [
    "PrincipalSpec",
    "PrincipalType",
    "default_principals",
    "ensure_default_principals",
    "entity_id_for",
    "principal_alias_to_id",
    "register_principal",
    "sha8",
    "slugify",
]
