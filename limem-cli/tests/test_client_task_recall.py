from __future__ import annotations

from limem.client import LimemClient
from limem.config import Credentials


def test_recall_for_task_posts_real_task_to_recall_endpoint(monkeypatch) -> None:
    calls = []
    client = LimemClient(creds=Credentials(api_key="k", db_id="db_1"))

    def _fake_request(method, path, *, json_body=None, params=None, timeout=None):
        calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "params": params,
                "timeout": timeout,
            }
        )
        return {
            "prompt_text": "## Relevant Memory\n- [Rule] use /recall",
            "items": [{"kind": "Rule", "text": "use /recall"}],
            "stats": {"selected": 1},
        }

    monkeypatch.setattr(client, "_request", _fake_request)

    result = client.recall_for_task(
        "请修复当前接口使用",
        limit=99,
        include_debug=True,
        timeout=0.25,
    )

    assert calls == [
        {
            "method": "POST",
            "path": "/db/db_1/recall",
            "json_body": {
                "task": "请修复当前接口使用",
                "limit": 20,
                "include_debug": True,
            },
            "params": None,
            "timeout": 0.25,
        }
    ]
    assert result.prompt_text.startswith("## Relevant Memory")
    assert result.items == [{"kind": "Rule", "text": "use /recall"}]
    assert result.stats == {"selected": 1}
