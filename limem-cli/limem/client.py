"""LimemClient — 唯一与 LiMem 后端契约耦合的模块。

后端契约（已通过生产 /openapi.json + API_DOC §0/§11 验证，2026-05-19）：

通用
- 多租户：所有数据操作走 /db/{db_id}/...，鉴权头 X-API-Key（缺失 401）
- Ingest body：{data: any, timestamp: int|null}（顶层无 metadata，元信息塞 data 内）
- Query body：{query, top_k}（**无 filters**，客户端必须二次过滤）
- summary 由 LLM 生成，不含原始 tag token；客户端需本地镜像 event_metadata

Entity / Pattern（v2 Breaking Change，2026-04 上线）
- 每个 entity 至多 1 篇 markdown 文档；卡片粒度 CRUD 已下线
- POST /api/entities 可内联 `pattern: {content: "..."}`（**单对象**，不是数组）
  旧 `patterns: [...]` 字段 → 422
- PUT /api/entities/{id}/patterns 整篇 upsert（不存在局部段落编辑）
- GET /api/entities/{id}/patterns  与
  GET /api/entities/{id}/patterns/recall?query=&mode=auto|full|section&top_k_sections=3
  返回同构 RecallEntityPatternResponse
- DELETE /api/entities/{id}/patterns 硬删（无 archive；404 表示该实体当前无 pattern）
- 无主检索 pipeline 消费 pattern；仅通过 /patterns/recall 独立召回（H2 切片打分）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

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
    """注册实体绑定的 markdown 档案（v2：每实体 ≤ 1 篇）。"""

    pattern_id: str
    entity_id: str
    content: str
    status: str = "active"
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_response(cls, p: dict[str, Any] | None) -> EntityPattern | None:
        if not p:
            return None
        return cls(
            pattern_id=p.get("id") or p.get("pattern_id") or "",
            entity_id=p.get("entity_id", ""),
            content=p.get("content", ""),
            status=p.get("status", "active"),
            created_at=int(p.get("created_at") or 0),
            updated_at=int(p.get("updated_at") or 0),
        )


@dataclass
class MatchedSection:
    heading: str
    score: float
    char_offset: int

    @classmethod
    def from_response(cls, s: dict[str, Any]) -> MatchedSection:
        return cls(
            heading=s.get("heading", ""),
            score=float(s.get("score") or 0.0),
            char_offset=int(s.get("char_offset") or 0),
        )


@dataclass
class PatternRecallResult:
    """`GET /patterns` 与 `GET /patterns/recall` 共享同构响应。"""

    mode: Literal["full", "section"]
    content: str
    total_chars: int
    matched_sections: list[MatchedSection] = field(default_factory=list)
    pattern: EntityPattern | None = None

    @classmethod
    def from_response(cls, r: dict[str, Any]) -> PatternRecallResult:
        mode = r.get("mode") or "full"
        return cls(
            mode=mode if mode in ("full", "section") else "full",
            content=r.get("content", "") or "",
            total_chars=int(r.get("total_chars") or 0),
            matched_sections=[MatchedSection.from_response(s) for s in (r.get("matched_sections") or [])],
            pattern=EntityPattern.from_response(r.get("pattern")),
        )

    def has_content(self) -> bool:
        return bool(self.content.strip())


@dataclass
class RegisterEntityResult:
    action: str  # "created" | "promoted" | "updated"
    existed_as_extracted: bool
    entity: dict[str, Any]
    pattern: EntityPattern | None  # v2：单对象（旧 patterns 数组已下线）


class LimemError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"LiMem {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class LimemClient:
    """同步 httpx 客户端；hook 脚本短期进程使用。

    长生命周期场景（MCP server）可调 ``aclient`` 取异步实例（未来实现）。
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

    def _require_db(self, db_id: str | None) -> str:
        db = db_id or self.creds.db_id
        if not db:
            raise LimemError(0, "db_id not configured")
        return db

    # ----- 健康 & 身份 -----

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def db_health(self, db_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/db/{self._require_db(db_id)}/health")

    def db_stats(self, db_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/db/{self._require_db(db_id)}/stats")

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
        db = self._require_db(db_id)
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
        db = self._require_db(db_id)
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

    def evolve(self, db_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/db/{self._require_db(db_id)}/evolve")

    # ----- Entity 注册 / 修改（v2） -----

    def entity_create_or_promote(
        self,
        entity_id: str,
        description: str,
        *,
        entity_type: str = "UNKNOWN",
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        pattern_markdown: str | None = None,
        db_id: str | None = None,
    ) -> RegisterEntityResult:
        """注册（或晋升 / 更新）注册实体。

        - `pattern_markdown` 不为空时，作为单对象 `pattern: {content}` 内联写入；失败由后端整体回滚。
        - 旧 `patterns: [...]` 数组参数已被后端拒绝（422）；本方法不再支持。
        """
        db = self._require_db(db_id)
        body: dict[str, Any] = {
            "entity_id": entity_id,
            "description": description,
            "entity_type": entity_type,
        }
        if aliases:
            body["aliases"] = aliases
        if metadata:
            body["metadata"] = metadata
        if pattern_markdown is not None and pattern_markdown.strip():
            body["pattern"] = {"content": pattern_markdown}
        r = self._request("POST", f"/db/{db}/api/entities", json_body=body)
        entity_dict = r.get("entity") or {}
        if isinstance(entity_dict, dict):
            if "id" in entity_dict and "entity_id" not in entity_dict:
                entity_dict = {**entity_dict, "entity_id": entity_dict["id"]}
            if "type" in entity_dict and "entity_type" not in entity_dict:
                entity_dict = {**entity_dict, "entity_type": entity_dict["type"]}
        return RegisterEntityResult(
            action=r.get("action", ""),
            existed_as_extracted=r.get("existed_as_extracted", False),
            entity=entity_dict,
            pattern=EntityPattern.from_response(r.get("pattern")),
        )

    def entity_patch(
        self,
        entity_id: str,
        *,
        description: str | None = None,
        entity_type: str | None = None,
        add_aliases: list[str] | None = None,
        remove_aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        db_id: str | None = None,
    ) -> RegisterEntityResult:
        db = self._require_db(db_id)
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if entity_type is not None:
            body["entity_type"] = entity_type
        if add_aliases:
            body["add_aliases"] = add_aliases
        if remove_aliases:
            body["remove_aliases"] = remove_aliases
        if metadata is not None:
            body["metadata"] = metadata
        if not body:
            raise LimemError(0, "entity_patch: empty payload")
        r = self._request("PATCH", f"/db/{db}/api/entities/{entity_id}", json_body=body)
        entity_dict = r.get("entity") or {}
        return RegisterEntityResult(
            action=r.get("action", "updated"),
            existed_as_extracted=r.get("existed_as_extracted", False),
            entity=entity_dict,
            pattern=EntityPattern.from_response(r.get("pattern")),
        )

    def entity_get(self, entity_id: str, *, db_id: str | None = None) -> dict[str, Any]:
        db = self._require_db(db_id)
        return self._request("GET", f"/db/{db}/api/entities/{entity_id}")

    def entity_list(self, *, db_id: str | None = None) -> dict[str, Any]:
        db = self._require_db(db_id)
        return self._request("GET", f"/db/{db}/api/entities")

    # ----- Entity Pattern markdown（v2） -----

    def patterns_upsert(
        self,
        entity_id: str,
        content: str,
        *,
        db_id: str | None = None,
    ) -> tuple[str, EntityPattern | None]:
        """整篇 upsert markdown；首次返回 action=created，之后 updated。"""
        db = self._require_db(db_id)
        if not content or not content.strip():
            raise LimemError(0, "patterns_upsert: content must not be blank")
        r = self._request(
            "PUT",
            f"/db/{db}/api/entities/{entity_id}/patterns",
            json_body={"content": content},
        )
        return (r.get("action", ""), EntityPattern.from_response(r.get("pattern")))

    def patterns_get(self, entity_id: str, *, db_id: str | None = None) -> PatternRecallResult:
        """读取 entity 的整篇 markdown（与 recall 同构响应）。空 pattern 返 200 + 空响应。"""
        db = self._require_db(db_id)
        r = self._request("GET", f"/db/{db}/api/entities/{entity_id}/patterns")
        return PatternRecallResult.from_response(r or {})

    def patterns_recall(
        self,
        entity_id: str,
        query: str,
        *,
        mode: Literal["auto", "full", "section"] = "auto",
        top_k_sections: int = 0,
        db_id: str | None = None,
        timeout: float | None = None,
    ) -> PatternRecallResult:
        """按 H2 切片打分召回；空 pattern 返 200 + 空响应。

        :param mode: ``auto`` (默认，content<2000 用 full 否则 section) / ``full`` / ``section``。
        :param top_k_sections: 0=后端默认 3；范围 [0, 20]。
        :param timeout: hook 内召回常 < hook_timeout_ms/2；可覆盖 self.timeout。
        """
        db = self._require_db(db_id)
        params: dict[str, Any] = {"query": query or "", "mode": mode}
        if top_k_sections:
            params["top_k_sections"] = top_k_sections
        r = self._request(
            "GET",
            f"/db/{db}/api/entities/{entity_id}/patterns/recall",
            params=params,
            timeout=timeout,
        )
        return PatternRecallResult.from_response(r or {})

    def patterns_delete(self, entity_id: str, *, db_id: str | None = None) -> EntityPattern | None:
        """硬删 entity 的 markdown；该实体当前无 pattern → 404 抛出。"""
        db = self._require_db(db_id)
        r = self._request("DELETE", f"/db/{db}/api/entities/{entity_id}/patterns")
        if isinstance(r, dict):
            return EntityPattern.from_response(r.get("pattern"))
        return None

    # ----- Graph 编辑（forget / fix 用） -----

    def graph_archive_event(self, event_id: str, *, db_id: str | None = None) -> dict[str, Any]:
        db = self._require_db(db_id)
        return self._request(
            "POST",
            f"/db/{db}/api/graph/delete",
            json_body={"memory_id": event_id, "kind": "event", "hard_delete": False},
        )

    def graph_update_event(
        self, event_id: str, fields: dict[str, Any], *, db_id: str | None = None
    ) -> dict[str, Any]:
        db = self._require_db(db_id)
        return self._request(
            "POST",
            f"/db/{db}/api/graph/update",
            json_body={"memory_id": event_id, "kind": "event", "fields": fields, "evolve": False},
        )
