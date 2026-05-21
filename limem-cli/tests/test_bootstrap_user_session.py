"""bootstrap_user_session 单元测试：0/1/N db 分支 + 鉴权失败 + select_db_id。"""

from __future__ import annotations

import json
import os

import pytest

from limem.bootstrap import (
    BootstrapResult,
    MultipleDatabasesError,
    bootstrap_user_session,
)
from limem.client import LimemError
from limem.config import Credentials

# ---------- 工具：用 monkeypatch 打桩 LimemClient ----------


class _FakeClient:
    """记录被调用次数与参数；按场景预设响应。"""

    def __init__(
        self,
        *,
        me_resp=None,
        me_exc: Exception | None = None,
        list_resp=None,
        list_exc: Exception | None = None,
        create_resp=None,
        create_exc: Exception | None = None,
    ):
        self.me_resp = me_resp if me_resp is not None else {}
        self.me_exc = me_exc
        self.list_resp = list_resp if list_resp is not None else []
        self.list_exc = list_exc
        self.create_resp = create_resp
        self.create_exc = create_exc
        self.calls: list[tuple[str, dict]] = []

    def me(self):
        self.calls.append(("me", {}))
        if self.me_exc:
            raise self.me_exc
        return self.me_resp

    def list_databases(self):
        self.calls.append(("list_databases", {}))
        if self.list_exc:
            raise self.list_exc
        return self.list_resp

    def create_database(self, display_name: str):
        self.calls.append(("create_database", {"display_name": display_name}))
        if self.create_exc:
            raise self.create_exc
        return self.create_resp


@pytest.fixture
def patch_client(monkeypatch):
    """返回一个工厂；调用 install(fake) 后任何 LimemClient(...) 都返回该 fake。"""

    def install(fake: _FakeClient) -> _FakeClient:
        from limem import bootstrap as bs_mod

        monkeypatch.setattr(bs_mod, "LimemClient", lambda **_kw: fake)
        return fake

    return install


# ---------- 主流程：0/1/N db 分支 ----------


def test_zero_db_auto_creates(patch_client):
    fake = patch_client(_FakeClient(
        list_resp=[],
        create_resp={"db_id": "db_new123", "display_name": "claude-code-personal"},
    ))
    r = bootstrap_user_session(
        base_url="https://example.invalid",
        api_key="user_token_abc",
        db_name="claude-code-personal",
    )
    assert isinstance(r, BootstrapResult)
    assert r.db_id == "db_new123"
    assert r.db_display_name == "claude-code-personal"
    assert r.db_action == "created"
    assert r.api_key == "user_token_abc"
    # 顺序：先 list 探活，再 create
    assert [c[0] for c in fake.calls] == ["list_databases", "me", "create_database"]
    assert fake.calls[2][1]["display_name"] == "claude-code-personal"


def test_single_db_reused_silently(patch_client):
    fake = patch_client(_FakeClient(
        list_resp=[{"db_id": "db_only", "display_name": "my-db"}],
    ))
    r = bootstrap_user_session(
        base_url="https://example.invalid",
        api_key="tok",
    )
    assert r.db_id == "db_only"
    assert r.db_action == "reused"
    # 关键：不该再调 create
    assert [c[0] for c in fake.calls] == ["list_databases", "me"]


def test_multi_db_without_picker_raises(patch_client):
    patch_client(_FakeClient(
        list_resp=[
            {"db_id": "db_a", "display_name": "alpha"},
            {"db_id": "db_b", "display_name": "beta"},
        ],
    ))
    with pytest.raises(MultipleDatabasesError) as excinfo:
        bootstrap_user_session(base_url="https://x", api_key="tok")
    assert len(excinfo.value.dbs) == 2


def test_multi_db_with_picker_selects(patch_client):
    patch_client(_FakeClient(
        list_resp=[
            {"db_id": "db_a", "display_name": "alpha"},
            {"db_id": "db_b", "display_name": "beta"},
        ],
    ))
    r = bootstrap_user_session(
        base_url="https://x",
        api_key="tok",
        picker=lambda dbs: 1,  # 选 beta
    )
    assert r.db_id == "db_b"
    assert r.db_action == "selected"


def test_select_db_id_match(patch_client):
    patch_client(_FakeClient(
        list_resp=[
            {"db_id": "db_a", "display_name": "alpha"},
            {"db_id": "db_b", "display_name": "beta"},
        ],
    ))
    r = bootstrap_user_session(
        base_url="https://x",
        api_key="tok",
        select_db_id="db_b",
    )
    assert r.db_id == "db_b"
    assert r.db_action == "selected"


def test_select_db_id_miss_raises(patch_client):
    patch_client(_FakeClient(
        list_resp=[{"db_id": "db_a", "display_name": "alpha"}],
    ))
    with pytest.raises(LimemError) as excinfo:
        bootstrap_user_session(
            base_url="https://x",
            api_key="tok",
            select_db_id="db_nonexistent",
        )
    assert excinfo.value.status == 404


def test_empty_api_key_rejected():
    with pytest.raises(LimemError):
        bootstrap_user_session(base_url="https://x", api_key="")


# ---------- 后端响应形态兼容性 ----------


def test_list_response_as_dict_with_databases_key(patch_client):
    """后端若返回 {databases: [...]} 也要兼容。"""
    fake = patch_client(_FakeClient(
        list_resp={
            "user_id": "u_42",
            "databases": [{"db_id": "db_x", "display_name": "x"}],
        },
    ))
    r = bootstrap_user_session(base_url="https://x", api_key="tok")
    assert r.db_id == "db_x"
    assert r.user_id == "u_42"
    assert r.db_action == "reused"
    assert [c[0] for c in fake.calls] == ["list_databases"]


def test_list_response_owner_user_id_fills_user_id_when_me_missing(patch_client):
    """后端 /databases 若只在列表项提供 owner_user_id，也要持久化 user identity。"""
    fake = patch_client(_FakeClient(
        me_resp={},
        list_resp=[
            {
                "db_id": "db_x",
                "display_name": "x",
                "owner_user_id": "owner_42",
            }
        ],
    ))
    r = bootstrap_user_session(base_url="https://x", api_key="tok")
    assert r.db_id == "db_x"
    assert r.user_id == "owner_42"
    assert [c[0] for c in fake.calls] == ["list_databases"]


def test_list_response_uses_id_field_fallback(patch_client):
    """后端若字段是 ``id`` 而非 ``db_id`` 也能识别。"""
    patch_client(_FakeClient(
        list_resp=[{"id": "db_fallback", "name": "the-name"}],
    ))
    r = bootstrap_user_session(base_url="https://x", api_key="tok")
    assert r.db_id == "db_fallback"
    assert r.db_display_name == "the-name"


# ---------- 鉴权失败 / 网络错误透传 ----------


def test_invalid_token_propagates(patch_client):
    patch_client(_FakeClient(
        list_exc=LimemError(401, "unauthorized"),
    ))
    with pytest.raises(LimemError) as excinfo:
        bootstrap_user_session(base_url="https://x", api_key="bad")
    assert excinfo.value.status == 401


# ---------- Credentials 兼容老 admin_token 残留 ----------


def test_credentials_from_dict_drops_unknown_keys():
    legacy = {
        "base_url": "https://x",
        "api_key": "k",
        "db_id": "d",
        "user_id": "u",
        "admin_token": "should-be-dropped",  # 老字段
        "weird_extra": 123,
    }
    c = Credentials.from_dict(legacy)
    # 序列化时 save() 只写 4 个白名单字段，残留字段不会回写
    payload = {
        "base_url": c.base_url,
        "api_key": c.api_key,
        "db_id": c.db_id,
        "user_id": c.user_id,
    }
    assert "admin_token" not in payload
    assert "weird_extra" not in payload
    assert c.api_key == "k"


def test_credentials_load_tolerates_legacy_keys(tmp_path, monkeypatch):
    """老 credentials.json 含 admin_token 残留时不应崩，且下次 save 自动清理。"""
    creds_path = tmp_path / "credentials.json"
    creds_path.write_text(json.dumps({
        "base_url": "https://legacy",
        "api_key": "legacy_key",
        "db_id": "legacy_db",
        "user_id": "legacy_user",
        "admin_token": "ADMIN_LEAK",
    }))
    monkeypatch.setattr("limem.config.USER_CREDENTIALS_PATH", creds_path)
    monkeypatch.setattr("limem.config.USER_CONFIG_DIR", tmp_path)

    c = Credentials.load()
    assert c.api_key == "legacy_key"
    assert c.db_id == "legacy_db"

    c.save()
    saved = json.loads(creds_path.read_text())
    assert "admin_token" not in saved
    assert set(saved.keys()) == {"base_url", "api_key", "db_id", "user_id"}
    # chmod 600
    mode = os.stat(creds_path).st_mode & 0o777
    assert mode == 0o600


def test_credentials_load_handles_corrupt_json(tmp_path, monkeypatch):
    """老 credentials.json 损坏时回退到默认值而非崩溃。"""
    creds_path = tmp_path / "credentials.json"
    creds_path.write_text("{not valid json")
    monkeypatch.setattr("limem.config.USER_CREDENTIALS_PATH", creds_path)
    monkeypatch.setattr("limem.config.USER_CONFIG_DIR", tmp_path)

    monkeypatch.delenv("LIMEM_API_KEY", raising=False)
    monkeypatch.delenv("LIMEM_DB_ID", raising=False)

    c = Credentials.load()
    assert c.api_key == ""
    assert c.db_id == ""
