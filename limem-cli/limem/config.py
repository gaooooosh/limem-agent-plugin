"""配置加载：环境变量 > credentials.json > .limem/local.json > ~/.config/limem/config.json > 默认值。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://limem.gaooooosh.art"

USER_CONFIG_DIR = Path(os.environ.get("LIMEM_CONFIG_DIR", "~/.config/limem")).expanduser()
USER_CREDENTIALS_PATH = USER_CONFIG_DIR / "credentials.json"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.json"
USER_CACHE_DIR = Path(os.environ.get("LIMEM_CACHE_DIR", "~/.cache/limem")).expanduser()
PATTERNS_DB_PATH = USER_CACHE_DIR / "patterns.sqlite"
HOOKS_LOG_PATH = USER_CACHE_DIR / "hooks.log"  # alias，与 EVENTS_LOG_PATH 同
EVENTS_LOG_PATH = HOOKS_LOG_PATH  # 新名：事件总线，daemon 消费来源
EVENTS_OFFSET_PATH = USER_CACHE_DIR / "events.offset"
SESSIONS_DIR = USER_CACHE_DIR / "sessions"

# daemon 相关
LIMEMD_SOCK_PATH = USER_CACHE_DIR / "limemd.sock"
LIMEMD_PID_PATH = USER_CACHE_DIR / "limemd.pid"
LIMEMD_FINGERPRINT_PATH = USER_CACHE_DIR / "limemd.sock.fingerprint"
LIMEMD_FORK_LOCK_PATH = USER_CACHE_DIR / "limemd.fork.lock"
LIMEMD_LOG_PATH = USER_CACHE_DIR / "limemd.log"

# 状态/缓存
STATUSLINE_CACHE_PATH = USER_CACHE_DIR / "statusline.cache.json"
SUGGESTIONS_PATH = USER_CACHE_DIR / "suggestions.json"
SUGGESTIONS_ARCHIVE_PATH = USER_CACHE_DIR / "suggestions.archive.ndjson"
PAUSE_PATH = USER_CACHE_DIR / "pause.json"
DEGRADED_SEEN_PATH = USER_CACHE_DIR / "degraded_seen.json"
SESSION_MUTE_PATH = USER_CACHE_DIR / "session_mute.json"
# 最近召回环形缓冲落盘（daemon 5s 原子写；daemon 重启 / 用户工具 fallback 读）
RECENT_RECALLS_PATH = USER_CACHE_DIR / "recent_recalls.json"
PENDING_RECALLS_PATH = USER_CACHE_DIR / "pending_recalls.json"

PROJECT_CONFIG_FILENAME = ".limem/local.json"


@dataclass
class Credentials:
    """加密敏感字段；永远不进 settings.json/项目目录。"""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    db_id: str = ""
    user_id: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> Credentials:
        """从字典构造（仅取白名单字段，未知键静默丢弃）。

        用于：load() 内部解析、单元测试、兼容老 credentials.json
        （如残留 ``admin_token`` 等已废弃字段）。
        """
        return cls(
            base_url=payload.get("base_url") or DEFAULT_BASE_URL,
            api_key=payload.get("api_key") or "",
            db_id=payload.get("db_id") or "",
            user_id=payload.get("user_id") or "",
        )

    @classmethod
    def load(cls) -> Credentials:
        env_key = os.environ.get("LIMEM_API_KEY")
        env_url = os.environ.get("LIMEM_BASE_URL")
        env_db = os.environ.get("LIMEM_DB_ID")
        env_user = os.environ.get("LIMEM_USER_ID")

        if USER_CREDENTIALS_PATH.exists():
            try:
                data = json.loads(USER_CREDENTIALS_PATH.read_text() or "{}")
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}

        return cls(
            base_url=env_url or data.get("base_url") or DEFAULT_BASE_URL,
            api_key=env_key or data.get("api_key") or "",
            db_id=env_db or data.get("db_id") or "",
            user_id=env_user or data.get("user_id") or "",
        )

    def save(self) -> None:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        USER_CREDENTIALS_PATH.write_text(
            json.dumps(
                {
                    "base_url": self.base_url,
                    "api_key": self.api_key,
                    "db_id": self.db_id,
                    "user_id": self.user_id,
                },
                indent=2,
            )
        )
        os.chmod(USER_CREDENTIALS_PATH, 0o600)


@dataclass
class RuntimeConfig:
    """非敏感配置，可进 settings.json 但默认放 ~/.config/limem/config.json。"""

    inject_budget_hard: int = 800
    inject_budget_pattern: int = 700  # principal markdown 切片独立预算
    inject_budget_soft: int = 1200
    soft_min_score: float = 0.6
    # v2 遗留键：本地 entity FTS candidate 召回已下线（v3 直接对 active principals 并发），
    # 保留字段防止旧 config.json 反序列化失败
    pattern_min_tokens: int = 2
    pattern_top_entities: int = 5
    # 单个 principal /patterns/recall 的 HTTP 超时（毫秒），并发期间任何一个超时即跳过
    patterns_recall_timeout_ms: int = 300
    # 每个 principal 的 markdown 召回最多取前 N 个 H2 段（后端默认 3，此处缩小避免噪声）
    patterns_recall_top_k_sections: int = 2
    hook_timeout_ms: int = 1500
    bm25_query_top_k: int = 20
    hard_recall_top_k: int = 100
    # v2：UserPromptSubmit 内 hard 召回的最低 importance 阈值（避免低分项挤掉 pattern/soft）
    hard_min_importance: float = 0.7
    cache_query_ttl_seconds: int = 300
    cache_query_max_entries: int = 200
    # Codex Stop hook 曾默认把整段 user/assistant 观察包送入 /ingest。
    # 这会让后端从 agent 自己的回答中抽取事件，形成自回归记忆污染；默认关闭。
    codex_session_observation_enabled: bool = False
    # 被动学习：只在本地可学习事件流 idle 后触发一次，避免周期性重复处理同一窗口。
    passive_learning_enabled: bool = True
    passive_learning_idle_seconds: int = 180
    passive_learning_auto_submit: bool = True
    passive_learning_min_events: int = 2
    passive_learning_assistant_evidence_chars: int = 500
    codex_stop_idle_seconds: int = 30
    # daemon / IPC
    daemon_connect_timeout_ms: int = 25
    daemon_call_timeout_ms: int = 200
    daemon_write_timeout_ms: int = 5000
    daemon_idle_close_seconds: int = 60
    daemon_rss_soft_limit_mb: int = 70
    statusline_cache_refresh_seconds: int = 5
    events_log_max_bytes: int = 50 * 1024 * 1024
    events_log_max_age_days: int = 7
    learner_period_seconds: int = 60
    learner_jaccard_threshold: float = 0.4
    learner_correction_window_hours: int = 24
    ngram_window_days: int = 7
    ngram_min_occurrences: int = 5
    ngram_min_accept_rate: float = 0.8
    suggestions_max_active: int = 500
    # PreToolUse / PostToolUse 配对窗口与采集截断（仅在 daemon 内存中使用）
    pre_post_pair_window_seconds: int = 60
    pre_tool_intent_chars: int = 200
    # 最近召回环形缓冲长度（statusline / dash 消费）
    recent_recalls_max: int = 20
    # statusline 是否在尾部追加 ✨ <短ID> 摘要
    statusline_last_recall_enabled: bool = True
    # statusline 摘要里最多列几个 short_id，超出用 (+N) 表示
    statusline_last_recall_short_ids_max: int = 2
    # UserPromptSubmit 中 transcript_path tail 采集的 assistant 上一轮回复截断长度
    prev_assistant_chars: int = 200
    # is_correction 改进版的最低置信度阈值（< 此值不入 correction buffer）
    is_correction_confidence_threshold: float = 0.5
    redact_patterns: list[str] = field(
        default_factory=lambda: [
            r"\bsk-[A-Za-z0-9]{20,}\b",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bBearer\s+[A-Za-z0-9._\-]+",
            r"-----BEGIN [A-Z ]+PRIVATE KEY-----",
        ]
    )

    @classmethod
    def load(cls) -> RuntimeConfig:
        if USER_CONFIG_PATH.exists():
            data = json.loads(USER_CONFIG_PATH.read_text())
        else:
            data = {}
        known = {k: data[k] for k in data if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_PATH.write_text(
            json.dumps({k: getattr(self, k) for k in self.__dataclass_fields__}, indent=2)
        )


@dataclass
class ProjectConfig:
    """项目级覆盖：scope/启用钩子等；不含凭证。"""

    project_id: str = ""
    enabled_hooks: list[str] = field(default_factory=list)

    @classmethod
    def discover(cls, start: Path | None = None) -> ProjectConfig | None:
        cur = (start or Path.cwd()).resolve()
        while True:
            candidate = cur / PROJECT_CONFIG_FILENAME
            if candidate.exists():
                data = json.loads(candidate.read_text())
                return cls(
                    project_id=data.get("project_id", ""),
                    enabled_hooks=data.get("enabled_hooks", []),
                )
            if cur.parent == cur:
                return None
            cur = cur.parent
