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


# 跨项目歧义的"本项目/this project"指代词。两处用途：
# 1) 从 project principal 的 aliases 中**剔除**这些字面量（否则多个项目都自称"本项目"）。
# 2) 在 remember_impl 写入前把文本内的这些指代词**替换**为当前项目 basename，避免 BM25
#    索引与召回注入文本里出现指代不明的串。
_PROJECT_DEICTICS: tuple[str, ...] = ("本项目", "当前项目", "this project")


def normalize_project_deictics(
    text: str,
    *,
    project_id: str,
    basename: str = "",
) -> str:
    """把文本中跨项目歧义的指代词替换为当前项目 basename。

    仅在 ``project_id`` 与 ``text`` 均非空时启用；否则原样返回。``basename`` 缺省时按
    ``_project_basename(project_id)`` 自算，再 fallback 到 ``project_id`` 全串。

    匹配是大小写不敏感的字面量替换；按字面量长度倒序处理，避免短串误吞长串。
    """
    if not text or not project_id:
        return text or ""
    label = basename or _project_basename(project_id) or project_id
    if not label:
        return text
    out = text
    # 长度倒序：保证 "this project" 先于潜在更短的英文指代词被替换
    for pat in sorted(_PROJECT_DEICTICS, key=len, reverse=True):
        out = re.sub(re.escape(pat), label, out, flags=re.IGNORECASE)
    return out


def default_principals(
    creds: Credentials | None,
    project_id: str,
    tool: str,
    *,
    include_user: bool = True,
    include_agent: bool = True,
    include_project: bool = True,
) -> list[PrincipalSpec]:
    """生成当前会话应该自动 ensure 的默认 principals。

    - 缺 ``user_id`` 时跳过 user（避免污染：sha8("") = 00000000）
    - ``project_id`` 缺则跳过 project
    - ``tool`` 缺则跳过 agent
    - include_* 用于区分主 Agent hook 与 daemon/MCP 等非观测入口
    """
    out: list[PrincipalSpec] = []
    user_id = (creds.user_id if creds else "") or ""
    if include_user and user_id:
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

    if include_agent and tool:
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

    if include_project and project_id:
        basename = _project_basename(project_id) or project_id
        # 只保留可唯一识别该项目的字符串；指代词（"本项目"/"当前项目"/"this project"）
        # 是跨项目歧义的，统一在 normalize_project_deictics 里做文本侧归一化。
        aliases = list({a for a in (project_id, basename) if a})
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


def _user_id_from_me_payload(payload: object) -> str:
    """Best-effort extract current user id from /me response variants."""
    if not isinstance(payload, dict):
        return ""
    for key in ("user_id", "uid", "id", "sub"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("user_id", "uid", "id", "sub"):
            val = user.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def ensure_current_user_principal(
    creds: Credentials | None,
    *,
    idx: EntityIndex,
    client: LimemClient | None = None,
    force: bool = False,
) -> str:
    """确保当前 LiMem 账号用户 principal 存在。

    这是 user principal 的专用入口：如果 credentials 缺 user_id，会尝试用当前
    API key 调 /me 补齐并回写 credentials。失败静默返回空串，绝不创建
    ``principal_user_00000000``。
    """
    if creds is None:
        return ""

    from .client import LimemClient as _LimemClient

    user_id = (creds.user_id or "").strip()
    if not user_id and creds.api_key:
        if client is None:
            client = _LimemClient(creds=creds, timeout=2.0)
        try:
            user_id = _user_id_from_me_payload(client.me())
        except Exception:
            user_id = ""
        if user_id:
            try:
                creds.user_id = user_id
                creds.save()
            except Exception:
                pass
    if not user_id:
        return ""

    spec = PrincipalSpec(
        principal_type="user",
        slug=user_id,
        description=f"当前 LiMem 账号用户：{user_id}",
        aliases=["我", "用户", "the user", "myself", user_id],
        scope="global",
        canonical=f"user:{user_id}",
    )
    eid = entity_id_for(spec)
    local_hit = None
    try:
        local_hit = idx.lookup_principal(eid)
    except Exception:
        local_hit = None
    backend_ok = bool((local_hit.raw_metadata or {}).get("backend_ok")) if local_hit else False
    if not force and local_hit is not None and backend_ok and _ensured_marker(eid).exists():
        return eid
    try:
        _ENSURE_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    register_principal(spec, creds=creds, idx=idx, client=client, swallow=True)
    try:
        row = idx.lookup_principal(eid)
        if row and bool((row.raw_metadata or {}).get("backend_ok")):
            _ensured_marker(eid).touch(exist_ok=True)
    except Exception:
        pass
    return eid


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
    include_user: bool = True,
    include_agent: bool = True,
    include_project: bool = True,
) -> list[str]:
    """幂等地确保默认 principals 在后端与本地都注册过。

    第一次成功后会落一个 mtime 文件（``~/.cache/limem/principals_ensured/<eid>``）做
    粗粒度去重；hook 多次触发也只重试本地缺失的项。后端失败永远 swallow。
    """
    out: list[str] = []
    specs = default_principals(
        creds,
        project_id,
        tool,
        include_user=include_user,
        include_agent=include_agent,
        include_project=include_project,
    )
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
        backend_ok = bool((local_hit.raw_metadata or {}).get("backend_ok")) if local_hit else False
        if not force and local_hit is not None and backend_ok and marker.exists():
            out.append(eid)
            continue
        register_principal(spec, creds=creds, idx=idx, client=client, swallow=True)
        try:
            row = idx.lookup_principal(eid)
            if row and bool((row.raw_metadata or {}).get("backend_ok")):
                marker.touch(exist_ok=True)
        except Exception:
            pass
        out.append(eid)
    return out


__all__ = [
    "PrincipalSpec",
    "PrincipalType",
    "default_principals",
    "ensure_current_user_principal",
    "ensure_default_principals",
    "entity_id_for",
    "normalize_project_deictics",
    "principal_alias_to_id",
    "register_principal",
    "sha8",
    "slugify",
]
