"""跨项目指代词去歧义：principal aliases + 写入文本归一化 + learner 候选文本。

回归用户提出的问题：N 个项目都把 "本项目"/"this project" 注册为 alias，跨项目时
这些别名无法唯一识别；用户主动 remember 文本里的"本项目"也会产生歧义召回。
"""

from __future__ import annotations

from limem.daemon.learner import build_candidate_text
from limem.daemon.writer import remember_impl
from limem.entity_index import EntityIndex
from limem.principals import default_principals, normalize_project_deictics

# ---------- normalize_project_deictics ----------


def test_normalize_replaces_all_deictic_forms() -> None:
    out = normalize_project_deictics(
        "本项目要用 docker。This Project should not run npm run dev. 当前项目的 CI 跑得慢。",
        project_id="github.com/foo/bar",
    )
    assert "本项目" not in out
    assert "当前项目" not in out
    assert "this project" not in out.lower()
    # 三处都替换为 basename
    assert out.count("bar") >= 3


def test_normalize_empty_project_id_is_passthrough() -> None:
    src = "本项目要用 docker"
    assert normalize_project_deictics(src, project_id="") == src


def test_normalize_empty_text_returns_empty() -> None:
    assert normalize_project_deictics("", project_id="foo/bar") == ""


def test_normalize_respects_explicit_basename() -> None:
    out = normalize_project_deictics(
        "本项目使用 Rust。",
        project_id="github.com/foo/bar",
        basename="my-app",
    )
    assert "my-app" in out
    assert "本项目" not in out


def test_normalize_does_not_double_replace_existing_basename() -> None:
    src = "bar 的部署用 docker，本项目也是。"
    out = normalize_project_deictics(src, project_id="github.com/foo/bar")
    # 原本含 "bar" 1 次；归一化把"本项目"再替换为"bar"，共 2 次；不应进一步膨胀
    assert out.count("bar") == 2
    assert "本项目" not in out


# ---------- default_principals aliases ----------


def test_project_principal_aliases_exclude_deictics() -> None:
    specs = default_principals(creds=None, project_id="github.com/foo/bar", tool="")
    project_specs = [s for s in specs if s.principal_type == "project"]
    assert len(project_specs) == 1
    aliases = set(project_specs[0].aliases)
    # 只保留可唯一识别的字符串
    assert aliases == {"github.com/foo/bar", "bar"}
    # 关键：不再含指代词
    assert "本项目" not in aliases
    assert "当前项目" not in aliases
    assert "this project" not in aliases


# ---------- remember_impl 端到端归一化 ----------


class _FakeIngestResult:
    def __init__(self, event_id: str, summary: str) -> None:
        self.event_id = event_id
        self.summary = summary
        self.is_new = True
        self.entities_created = 0
        self.event_count = 1


class _FakeClient:
    def __init__(self) -> None:
        self.ingest_calls: list[dict] = []

    def ingest(self, data, *, timestamp=None):  # noqa: ARG002
        self.ingest_calls.append(data)
        return _FakeIngestResult(
            event_id=f"evt_{len(self.ingest_calls):08d}",
            summary=(data.get("text") or "")[:100],
        )

    # 兼容 ensure_default_principals 内部可能调到的端点；本测试不关心
    def entity_create_or_promote(self, *args, **kwargs):  # noqa: ARG002
        return {"ok": True}

    def entity_patch(self, *args, **kwargs):  # noqa: ARG002
        return {"ok": True}


class _FakeCreds:
    api_key = "k"
    db_id = "db_1"
    user_id = "u_42"


def _patch(monkeypatch, fake_client: _FakeClient) -> None:
    from limem import principals as pmod
    from limem.daemon import writer as wmod

    monkeypatch.setattr(wmod, "LimemClient", lambda **_kw: fake_client)

    def _noop_register(spec, *, creds, idx, client=None, swallow=True):  # noqa: ARG001
        from limem.principals import entity_id_for

        return entity_id_for(spec)

    monkeypatch.setattr(pmod, "register_principal", _noop_register)


def test_remember_normalizes_deictic_text_in_project_scope(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    out = remember_impl(
        text="本项目要使用 docker rebuild，不要 npm run dev。",
        scope="project:github.com/foo/bar",
        mem_type="rule",
        project_id="github.com/foo/bar",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    meta = idx.lookup_event(out["event_id"])
    assert meta is not None
    original = meta.raw_metadata.get("original_text", "")
    assert "本项目" not in original
    assert "bar" in original

    # composed_text（带 tag block）作为 BM25 输入，同样应归一化
    composed = fake.ingest_calls[0]["text"]
    assert "本项目" not in composed
    assert "bar" in composed


def test_remember_does_not_normalize_in_global_scope(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    out = remember_impl(
        text="本项目通常表示一种抽象语境。",
        scope="global",
        mem_type="fact",
        project_id="",  # global scope 下也允许传 project_id；本测试明确不传
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    meta = idx.lookup_event(out["event_id"])
    assert meta is not None
    # global scope 下保持原文，不做指代替换
    assert "本项目" in meta.raw_metadata.get("original_text", "")


def test_remember_normalizes_user_detail_in_project_scope(monkeypatch, tmp_path) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)
    idx = EntityIndex(db_path=tmp_path / "patterns.sqlite")

    remember_impl(
        text="禁止直接启动 dev server。",
        scope="project:github.com/foo/bar",
        mem_type="rule",
        project_id="github.com/foo/bar",
        detail="本项目要求所有 dev workflow 走 Docker。",
        creds=_FakeCreds(),
        idx=idx,
        skip_redact=True,
    )

    detail = fake.ingest_calls[0]["detail"]
    # 用户传入的 detail 中"本项目"已归一化（中英直接相连，无空格）
    assert "本项目" not in detail
    assert "bar要求" in detail
    # build_natural_detail 自带的 provenance 句保留完整 project_id
    assert "当前项目是 github.com/foo/bar" in detail


# ---------- learner build_candidate_text ----------


def test_build_candidate_text_with_project_label() -> None:
    cluster = [{"prompt": "不要 npm run dev，应该 docker rebuild"}]
    out = build_candidate_text(cluster, project_label="repo")
    assert out.startswith("在 repo 中")
    assert "本项目" not in out


def test_build_candidate_text_without_label_falls_back() -> None:
    cluster = [{"prompt": "不要 npm run dev，应该 docker rebuild"}]
    out = build_candidate_text(cluster, project_label="")
    assert out.startswith("在当前会话中")
    assert "本项目" not in out


def test_build_candidate_text_empty_cluster() -> None:
    out = build_candidate_text([], project_label="repo")
    assert out == "在 repo 中，遵循用户最近反复纠正的工作方式。"
