"""User bootstrap：用用户 API key 接入 LiMem，解析或建 db_id，落盘凭证。

完全无 admin 路径——用户应已在 LiMem dashboard 拥有账户与 API key。
本模块只关心：用 key 探活 → 解析/创建唯一 db → 返回结构化结果（调用方写凭证）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .client import LimemClient, LimemError
from .config import Credentials


@dataclass
class BootstrapResult:
    """bootstrap 流程的结构化产物；调用方据此写 ``Credentials``。"""

    user_id: str
    api_key: str
    db_id: str
    db_display_name: str
    db_action: str  # "reused" | "created" | "selected"
    available_dbs: list[dict[str, Any]] = field(default_factory=list)


class MultipleDatabasesError(LimemError):
    """用户拥有多个 db，且未传 ``select_db_id`` 也未提供交互式 picker。"""

    def __init__(self, dbs: list[dict[str, Any]]):
        super().__init__(
            0,
            f"multiple databases found ({len(dbs)}); pass select_db_id or supply a picker",
        )
        self.dbs = dbs


# ---------- 工具：兼容后端响应的多种形态 ----------


def _normalize_db_listing(resp: Any) -> list[dict[str, Any]]:
    """``list_databases()`` 响应结构 best-effort 兼容（list / {databases:[]} / 单 dict）。"""
    if isinstance(resp, list):
        return [d for d in resp if isinstance(d, dict)]
    if isinstance(resp, dict):
        for key in ("databases", "items", "results"):
            v = resp.get(key)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
        # 单个 db dict 形态
        if _db_id_of(resp):
            return [resp]
    return []


def _db_id_of(db: dict[str, Any]) -> str:
    return db.get("db_id") or db.get("id") or ""


def _db_name_of(db: dict[str, Any]) -> str:
    return db.get("display_name") or db.get("name") or ""


def _user_id_of_listing(resp: Any) -> str:
    """``list_databases()`` 响应顶层若含 ``user_id`` 则提取，否则空。"""
    if isinstance(resp, dict):
        return resp.get("user_id") or resp.get("uid") or ""
    return ""


# ---------- 主流程 ----------


def bootstrap_user_session(
    *,
    base_url: str,
    api_key: str,
    db_name: str = "claude-code-personal",
    select_db_id: str | None = None,
    picker: Callable[[list[dict[str, Any]]], int] | None = None,
) -> BootstrapResult:
    """用用户 API key 接入 LiMem，自动解析或创建 db。

    流程：
      1. 用 ``api_key`` 调 ``list_databases()`` 作为 token 有效性探针
      2. 解析 db：
         - 命中 ``select_db_id`` → 选中
         - 0 个 → 自动 ``create_database(db_name)``
         - 1 个 → 静默使用
         - N 个 → 若 ``picker`` 提供则交互式选；否则抛 ``MultipleDatabasesError``
      3. 返回 ``BootstrapResult``；**不**自动落盘 ``Credentials``——这是调用方职责
    """
    if not api_key:
        raise LimemError(0, "api_key required")

    client = LimemClient(
        creds=Credentials(base_url=base_url, api_key=api_key)
    )

    # 拉库列表（兼任 token 有效性探针：无效 key 后端返回 401/403）
    resp = client.list_databases()
    dbs = _normalize_db_listing(resp)
    user_id = _user_id_of_listing(resp)

    # 显式指定 select_db_id：必须在已有 db 列表内
    if select_db_id:
        for db in dbs:
            if _db_id_of(db) == select_db_id:
                return BootstrapResult(
                    user_id=user_id,
                    api_key=api_key,
                    db_id=select_db_id,
                    db_display_name=_db_name_of(db),
                    db_action="selected",
                    available_dbs=dbs,
                )
        raise LimemError(
            404,
            f"select_db_id={select_db_id!r} not in your databases",
        )

    if len(dbs) == 0:
        new_db = client.create_database(display_name=db_name)
        new_id = _db_id_of(new_db) if isinstance(new_db, dict) else ""
        if not new_id:
            raise LimemError(0, f"create_database response missing db_id: {new_db}")
        return BootstrapResult(
            user_id=user_id,
            api_key=api_key,
            db_id=new_id,
            db_display_name=_db_name_of(new_db) or db_name,
            db_action="created",
            available_dbs=[new_db],
        )

    if len(dbs) == 1:
        db = dbs[0]
        db_id = _db_id_of(db)
        if not db_id:
            raise LimemError(0, f"existing database missing id: {db}")
        return BootstrapResult(
            user_id=user_id,
            api_key=api_key,
            db_id=db_id,
            db_display_name=_db_name_of(db),
            db_action="reused",
            available_dbs=dbs,
        )

    # 多 db：必须有 picker 才能选
    if picker is None:
        raise MultipleDatabasesError(dbs)
    idx = picker(dbs)
    if idx < 0 or idx >= len(dbs):
        raise LimemError(0, f"picker returned invalid index: {idx}")
    db = dbs[idx]
    db_id = _db_id_of(db)
    if not db_id:
        raise LimemError(0, f"selected database missing id: {db}")
    return BootstrapResult(
        user_id=user_id,
        api_key=api_key,
        db_id=db_id,
        db_display_name=_db_name_of(db),
        db_action="selected",
        available_dbs=dbs,
    )
