"""Current LiMem account identity helpers."""

from __future__ import annotations

from typing import Protocol


class IdentityClient(Protocol):
    def me(self) -> object: ...

    def list_databases(self) -> object: ...


_USER_ID_KEYS = ("user_id", "uid", "id", "sub", "owner_user_id")


def user_id_from_payload(payload: object) -> str:
    """Extract a stable current-user id from common service response shapes."""
    if isinstance(payload, dict):
        for key in _USER_ID_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for nested_key in ("user", "account", "owner"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                val = user_id_from_payload(nested)
                if val:
                    return val
        for list_key in ("databases", "items", "results"):
            val = user_id_from_database_listing(payload.get(list_key))
            if val:
                return val
    return user_id_from_database_listing(payload)


def user_id_from_database_listing(payload: object) -> str:
    """Extract owner user id from a database list if all visible dbs agree."""
    if isinstance(payload, dict):
        for key in ("databases", "items", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return user_id_from_database_listing(val)
        return ""
    if not isinstance(payload, list):
        return ""

    ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key in ("owner_user_id", "user_id", "uid"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                ids.add(val.strip())
                break
    return next(iter(ids)) if len(ids) == 1 else ""


def resolve_current_user_id(
    client: IdentityClient,
    *,
    me_payload: object | None = None,
    database_listing: object | None = None,
) -> str:
    """Resolve current LiMem account id through identity endpoints."""
    if database_listing is not None:
        user_id = user_id_from_payload(database_listing)
        if user_id:
            return user_id

    if me_payload is None:
        try:
            me_payload = client.me()
        except Exception:
            me_payload = None
    user_id = user_id_from_payload(me_payload)
    if user_id:
        return user_id

    if database_listing is None:
        try:
            database_listing = client.list_databases()
        except Exception:
            database_listing = None
    return user_id_from_payload(database_listing)
