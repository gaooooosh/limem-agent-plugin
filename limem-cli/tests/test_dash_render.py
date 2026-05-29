"""dash 多视图渲染：空 / 有数据 / daemon 不可达占位。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from rich.console import Console

from limem.dash import app


def _text(renderable) -> str:
    c = Console(width=120)
    with c.capture() as cap:
        c.print(renderable)
    return cap.get()


def test_tabs_highlight_current_view() -> None:
    out = _text(app._render_tabs("active"))
    assert "活跃记忆" in out
    assert "实时召回" in out


def test_recalls_none_shows_daemon_off() -> None:
    assert "daemon off" in _text(app._render_recalls_table(None))


def test_recalls_empty_shows_placeholder() -> None:
    assert "暂无召回记录" in _text(app._render_recalls_table([]))


def test_recalls_with_data_renders_rows() -> None:
    recalls = [
        {
            "ts": 0,
            "project_id": "proj/demo",
            "items": [
                {"src": "hard", "summary_head": "部署前跑测试"},
                {"src": "bm25", "summary_head": "docker"},
            ],
        }
    ]
    out = _text(app._render_recalls_table(recalls))
    assert "proj/demo" in out
    assert "部署前跑测试" in out


def test_active_none_shows_warning() -> None:
    assert "无法读取" in _text(app._render_active_table(None))


def test_active_empty_shows_hint() -> None:
    assert "暂无活跃规则" in _text(app._render_active_table(([], [])))


def test_active_with_data_renders() -> None:
    meta = SimpleNamespace(
        raw_metadata={"original_text": "不启动 dev server"},
        summary="",
        mem_type="rule",
        scope="project:demo",
        importance=0.9,
    )
    principal = SimpleNamespace(
        principal_type="project", scope="project:demo", canonical="demo", slug="demo"
    )
    out = _text(app._render_active_table(([meta], [principal])))
    assert "不启动 dev server" in out
    assert "档案·project" in out


def test_activity_none_and_empty() -> None:
    assert "尚未生成" in _text(app._render_activity_panel(None))
    assert "暂无活动事件" in _text(app._render_activity_panel([]))


def test_activity_with_lines() -> None:
    out = _text(app._render_activity_panel(["12:00:00  user_prompt_submit  hi"]))
    assert "user_prompt_submit" in out


def test_fetch_recent_recalls_falls_back_to_disk(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app.daemon_client, "list_recent_recalls", lambda limit: None)
    path = tmp_path / "recent.json"
    path.write_text(json.dumps({"records": [{"ts": 1, "items": []}]}))
    monkeypatch.setattr(app, "RECENT_RECALLS_PATH", path)
    out = app._fetch_recent_recalls(20)
    assert out and out[0]["ts"] == 1


def test_fetch_recent_recalls_none_when_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app.daemon_client, "list_recent_recalls", lambda limit: None)
    monkeypatch.setattr(app, "RECENT_RECALLS_PATH", tmp_path / "missing.json")
    assert app._fetch_recent_recalls(20) is None


def test_read_events_tail_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app, "EVENTS_LOG_PATH", tmp_path / "none.log")
    assert app._read_events_tail() is None
