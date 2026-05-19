"""A2：`limem hook claude-code PreToolUse` 不被 Click choice 拒绝。

修复点位：limem-cli/limem/cli.py 的 hook 子命令 Click.Choice
"""

from __future__ import annotations

from click.testing import CliRunner

from limem.cli import main


def test_hook_pre_tool_use_accepted_by_click() -> None:
    """传 stdin 空 JSON，hook 应正常退出 0；如果 Click 拒绝会返回非 0 退出码。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "PreToolUse"], input="{}")
    # exit_code 不为 2（Click usage error）就说明 choice 接受了
    assert result.exit_code != 2, (
        f"Click 拒绝了 PreToolUse choice，输出:\n{result.output}"
    )


def test_hook_post_tool_use_still_accepted() -> None:
    """回归保护：原有 PostToolUse 仍可用。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "PostToolUse"], input="{}")
    assert result.exit_code != 2


def test_hook_unknown_event_rejected() -> None:
    """未知事件名仍应被 Click 拒绝（exit_code=2 是 Click usage error）。"""
    runner = CliRunner()
    result = runner.invoke(main, ["hook", "claude-code", "NonexistentEvent"], input="{}")
    assert result.exit_code == 2
