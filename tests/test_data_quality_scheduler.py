"""tests/test_data_quality_scheduler.py — Ch2 scheduler 模組。"""

from unittest.mock import patch


def test_scheduler_enabled_returns_false_by_default(monkeypatch):
    """DATA_QUALITY_ENABLED 環境變數未設時應 False。"""
    monkeypatch.delenv("DATA_QUALITY_ENABLED", raising=False)
    from config import reset_for_tests

    reset_for_tests()

    from services.data_quality_scheduler import scheduler_enabled

    assert scheduler_enabled() is False


def test_scheduler_enabled_returns_true_when_env_set(monkeypatch):
    """DATA_QUALITY_ENABLED=true → True。"""
    monkeypatch.setenv("DATA_QUALITY_ENABLED", "true")
    from config import reset_for_tests

    reset_for_tests()

    from services.data_quality_scheduler import scheduler_enabled

    assert scheduler_enabled() is True


def test_run_data_quality_once_orchestrates_engine_and_dispatch(test_db_session):
    """run_data_quality_once：跑 engine.run_all_rules → dispatch.emit → flush_line_digest。
    回傳 dict 含 detected/new_open/ran_at。
    """
    from services.data_quality._base import Violation
    from services.data_quality_scheduler import run_data_quality_once

    fake_v = Violation(
        rule_code="x", severity="P0", entity_type="e", entity_id="1", summary="s"
    )

    with (
        patch(
            "services.data_quality_scheduler.run_all_rules", return_value=[fake_v]
        ) as m_run,
        patch("services.data_quality_scheduler.emit", return_value=True) as m_emit,
        patch("services.data_quality_scheduler.flush_line_digest") as m_flush,
    ):
        result = run_data_quality_once()

    assert m_run.called
    assert m_emit.called
    assert m_flush.called
    assert result["detected"] == 1
    assert result["new_open"] == 1
    assert "ran_at" in result


def test_run_data_quality_once_returns_zero_on_no_violations(test_db_session):
    """空 violation list → detected=0, new_open=0。"""
    from services.data_quality_scheduler import run_data_quality_once

    with (
        patch("services.data_quality_scheduler.run_all_rules", return_value=[]),
        patch("services.data_quality_scheduler.flush_line_digest"),
    ):
        result = run_data_quality_once()

    assert result["detected"] == 0
    assert result["new_open"] == 0
