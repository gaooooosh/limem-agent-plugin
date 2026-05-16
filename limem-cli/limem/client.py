"""LimemClient — 唯一与 LiMem 后端契约耦合的模块。

后端契约（已通过生产 /openapi.json 验证，2026-05-15）：
- 多租户：所有数据操作走 /db/{db_id}/...
- 强制鉴权：X-API-Key 头（缺失 401）
- Ingest schema：{data: any, timestamp: int|null}（顶层无 metadata，元信息塞 data 内）
- Query schema：{query, top_k}（无 filters，客户端必须二次过滤）
- Pattern API 后端原生：/db/{db_id}/api/entities 注册 entity 时可一并提交 patterns
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Credentials


@dataclass
class IngestResult:
    event_id: str
    summary: str
    is_new: bool
    entities_created: int
    event_count: int


@dataclass
class QueryResult:
    event_id: str
    summary: str
    action: str
    causality: str
    timestamp: int
    score: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityPattern:
    pattern_id: str  # 后端响应里字段名是 ``id``；本地统一叫 pattern_id 更清晰
    content: str
    pattern_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    entity_id: str = ""
    status: str = ""

    @classmethod
    def from_response(cls, p: dict[str, Any]) -> EntityPattern:
        return cls(
            pattern_id=p.get("id") or p.get("pattern_id") or "",
            content=p.get("content", ""),
            pattern_type=p.get("pattern_type", ""),
            metadata=p.get("metadata", {}) or {},
            entity_id=p.get("entity_id", ""),
            status=p.get("status", ""),
        )


@dataclass
class RegisterEntityResult:
    action: str  # "created" | "updated"
    existed_as_extracted: bool
    entity: dict[str, Any]
    patterns: list[EntityPattern]


class LimemError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"LiMem {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class LimemClient:
    """同步 httpx 客户端；hook 脚本短期进程使用。

    长生命周期场景（MCP server）可调 ``aclient`` 取异步实例。
    """

    def __init__(
        self,
        creds: Credentials | None = None,
        *,
        timeout: float = 10.0,
        ingest_timeout: float = 90.0,
    ) -> None:
        self.creds = creds or Credentials.load()
        self.timeout = timeout
        self.ingest_timeout = ingest_timeout

    # ----- 基础 -----

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "limem-cli/0.1"}
        if self.creds.api_key:
            h["X-API-Key"] = self.creds.api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self.creds.base_url.rstrip('/')}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self._url(path)
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            with httpx.Client(timeout=effective_timeout) as c:
                r = c.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=self._headers(),
                )
        except Exception as e:
            # 网络异常：best-effort 通知 daemon
            self._notify_connectivity(0, str(e)[:60])
            raise LimemError(0, f"network error: {e}", None) from e

        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("detail") if isinstance(body, dict) else str(body)
            except Exception:
                body = r.text
                msg = r.text[:200]
            self._notify_connectivity(r.status_code, msg or "")
            raise LimemError(r.status_code, msg or "request failed", body)

        # 成功
        self._notify_connectivity(200, None, ok=True)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    @staticmethod
    def _notify_connectivity(status: int, reason: str | None, *, ok: bool = False) -> None:
        """阶段 3：通知 daemon 连通性状态。失败静默（client.py 是 P5 单点，唯一允许这里写状态）。"""
        try:
            from . import daemon_client as _dc
            _dc.set_connectivity(status=status, reason=reason, ok=ok)
        except Exception:
            pass

    # ----- 健康 & 身份 -----

    def me(self) -> dict[str, Any]:
        """返回当前 API Key 关联的 user 信息。"""
        return self._request("GET", "/me")

    def db_health(self, db_id: str | None = None) -> dict[str, Any]:
        db = db_id or self.creds.db_id
        if not db:
            raise LimemError(0, "db_id not configured")
        return self._request("GET", f"/db/{db}/health")

    # ----- 用户级库管理 -----

    def list_databases(self) -> dict[str, Any]:
        return self._request("GET", "/databases")

    def create_database(self, display_name: str) -> dict[str, Any]:
        return self._request("POST", "/databases", json_body={"display_name": display_name})

    # ----- Ingest / Query -----

    def ingest(
        self,
        data: dict[str, Any],
        *,
        timestamp: int | None = None,
        db_id: str | None = None,
    ) -> IngestResult:
        """data 内自由放置 metadata，后端 LLM 会读 data['detail'] 或 data['text']。"""
        db = db_id or self.creds.db_id
        if not db:
            raise LimemError(0, "db_id not configured")
        body = {"data": data, "timestamp": timestamp if timestamp is not None else int(time.time())}
        r = self._request("POST", f"/db/{db}/ingest", json_body=body, timeout=self.ingest_timeout)
        return IngestResult(
            event_id=r["event_id"],
            summary=r.get("summary", ""),
            is_new=r.get("is_new", False),
            entities_created=r.get("entities_created", 0),
            event_count=r.get("event_count", 0),
        )

    def query(
        self,
        query: str,
        *,
        top_k: int = 20,
        db_id: str | None = None,
    ) -> list[QueryResult]:
        db = db_id or self.creds.db_id
        if not db:
            raise LimemError(0, "db_id not configured")
        body = {"query": query, "top_k": top_k}
        r = self._request("POST", f"/db/{db}/query", json_body=body)
        out: list[QueryResult] = []
        for item in r.get("results", []):
            out.append(
                QueryResult(
                    event_id=item.get("event_id", ""),
                    summary=item.get("summary", ""),
                    action=item.get("action", ""),
                    causality=item.get("causality", ""),
                    timestamp=item.get("timestamp", 0),
                    score=item.get("score", 0.0),
                    raw=item,
                )
            )
        return out

    # ----- Entity / Pattern -----

    def register_entity(
        self,
        entity_id: str,
        description: str,
        *,
        entity_type: str = "UNKNOWN",
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        patterns: list[dict[str, Any]] | None = None,
        db_id: str | None = None,
    ) -> RegisterEntityResult:
        db = db_id or self.creds.db_id
        body: dict[str, Any] = {
            "entity_id": entity_id,
            "description": description,
            "entity_type": entity_type,
        }
        if aliases:
            body["aliases"] = aliases
        if metadata:
            body["metadata"] = metadata
        if patterns:
            body["patterns"] = patterns
        r = self._request("POST", f"/db/{db}/api/entities", json_body=body)
        entity_dict = r.get("entity") or {}
        # 后端用 ``id``+``type``，本地统一 ``entity_id``+``entity_type``
        if isinstance(entity_dict, dict):
            if "id" in entity_dict and "entity_id" not in entity_dict:
                entity_dict = {**entity_dict, "entity_id": entity_dict["id"]}
            if "type" in entity_dict and "entity_type" not in entity_dict:
                entity_dict = {**entity_dict, "entity_type": entity_dict["type"]}
        return RegisterEntityResult(
            action=r.get("action", ""),
            existed_as_extracted=r.get("existed_as_extracted", False),
            entity=entity_dict,
            patterns=[EntityPattern.from_response(p) for p in r.get("patterns", [])],
        )

    def list_entity_patterns(
        self, entity_id: str, *, db_id: str | None = None
    ) -> list[EntityPattern]:
        db = db_id or self.creds.db_id
        r = self._request("GET", f"/db/{db}/api/entities/{entity_id}/patterns")
        # 后端真实形态：{"items": [...]}；老接口/降级形态：直接是 list 或 {"patterns": [...]}
        if isinstance(r, list):
            items = r
        elif isinstance(r, dict):
            items = r.get("items") or r.get("patterns") or []
        else:
            items = []
        return [EntityPattern.from_response(p) for p in items]

    def create_entity_pattern(
        self,
        entity_id: str,
        content: str,
        *,
        pattern_type: str = "preference",
        metadata: dict[str, Any] | None = None,
        pattern_id: str | None = None,
        db_id: str | None = None,
    ) -> EntityPattern:
        db = db_id or self.creds.db_id
        body: dict[str, Any] = {"content": content, "pattern_type": pattern_type}
        if metadata:
            body["metadata"] = metadata
        if pattern_id:
            body["pattern_id"] = pattern_id
        r = self._request("POST", f"/db/{db}/api/entities/{entity_id}/patterns", json_body=body)
        p = r.get("pattern", r) if isinstance(r, dict) else {}
        return EntityPattern.from_response(p)

    def batch_create_entity_patterns(
        self,
        entity_id: str,
        patterns: list[dict[str, Any]],
        *,
        db_id: str | None = None,
    ) -> list[EntityPattern]:
        db = db_id or self.creds.db_id
        r = self._request(
            "POST",
            f"/db/{db}/api/entities/{entity_id}/patterns/:batch",
            json_body={"patterns": patterns},
        )
        if isinstance(r, list):
            items = r
        elif isinstance(r, dict):
            items = r.get("items") or r.get("patterns") or []
        else:
            items = []
        return [EntityPattern.from_response(p) for p in items]

    def delete_entity_pattern(
        self, entity_id: str, pattern_id: str, *, db_id: str | None = None
    ) -> None:
        db = db_id or self.creds.db_id
        self._request("DELETE", f"/db/{db}/api/entities/{entity_id}/patterns/{pattern_id}")

    # ----- Graph 编辑（forget 用） -----

    def graph_archive_event(self, event_id: str, *, db_id: str | None = None) -> dict[str, Any]:
        db = db_id or self.creds.db_id
        return self._request(
            "POST",
            f"/db/{db}/api/graph/delete",
            json_body={"memory_id": event_id, "kind": "event", "hard_delete": False},
        )

    def graph_update_event(
        self, event_id: str, fields: dict[str, Any], *, db_id: str | None = None
    ) -> dict[str, Any]:
        db = db_id or self.creds.db_id
        return self._request(
            "POST",
            f"/db/{db}/api/graph/update",
            json_body={"memory_id": event_id, "kind": "event", "fields": fields, "evolve": False},
        )
