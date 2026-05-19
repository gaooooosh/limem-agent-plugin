"""A4.2：score_correction 置信度的四种典型组合断言。"""

from __future__ import annotations

from limem.daemon.learner import is_correction, score_correction


def test_is_correction_backward_compat_returns_bool() -> None:
    """旧 API 保留：is_correction 仍返回 bool，未被 score_correction 取代。"""
    assert is_correction("不对，应该用 docker") is True
    assert is_correction("Don't use npm dev") is True
    assert is_correction("hello world") is False


def test_score_correction_no_keyword_returns_false() -> None:
    ok, conf = score_correction("hello world")
    assert ok is False
    assert conf == 0.0


def test_score_correction_keyword_only_baseline() -> None:
    """仅命中纠正词、无 prev_assistant、无强否定词 → 基础 0.5。"""
    ok, conf = score_correction("应该用 docker")  # 仅 prefer hint，无强否定
    assert ok is True
    assert conf == 0.5


def test_score_correction_negative_hint_bumps_score() -> None:
    """强否定词加 0.15。"""
    ok, conf = score_correction("不对，应该用 docker")
    assert ok is True
    # 基础 0.5 + 否定 0.15 = 0.65
    assert abs(conf - 0.65) < 1e-6


def test_score_correction_with_prev_assistant_bumps_more() -> None:
    """有 prev_assistant 加 0.25。"""
    ok, conf = score_correction(
        "应该用 docker",
        prev_assistant="I will run npm run dev",
    )
    assert ok is True
    # 0.5 + 0.25（有 prev）= 0.75
    assert conf >= 0.75
    assert conf <= 1.0


def test_score_correction_with_topic_overlap_bumps_most() -> None:
    """prev_assistant 与 prompt 主题重叠 → 再加 0.1。"""
    ok, conf = score_correction(
        "不对，应该用 docker rebuild",
        prev_assistant="I will run docker rebuild now",
    )
    assert ok is True
    # 0.5 + 否定 0.15 + prev 0.25 + topic 0.1 = 1.0（封顶）
    assert conf == 1.0


def test_score_correction_capped_at_one() -> None:
    """所有加成全开也不超过 1.0。"""
    _, conf = score_correction(
        "不对，don't 改成 docker rebuild",
        prev_assistant="I will run docker rebuild now don't worry",
    )
    assert conf == 1.0
