"""排程器連續失敗 LINE 告警測試。

現況問題：scheduler_iteration 連續失敗達 ALERT_THRESHOLD(3) 只 capture_exception
（Sentry 預設無 DSN = no-op）→ 排程崩潰無人知。接上 services/ops_alert 的 LINE
推播基建（OPS_ALERT_LINE_GROUP_ID 未設則 no-op）。

告警節流：只在「跨過 threshold 那一次」與其後 3 的冪次（3/9/27/81…）告警，
不可每次迭代重複轟炸。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config import settings
from services import ops_alert
from utils import scheduler_observability as so


@pytest.fixture(autouse=True)
def _reset_state():
    so.reset_for_tests()
    ops_alert.reset_for_tests()
    yield
    so.reset_for_tests()
    ops_alert.reset_for_tests()


@pytest.fixture
def _restore_group_id():
    original = settings.ops_alert.line_group_id
    yield
    settings.ops_alert.line_group_id = original


def _fail_once(name="sched_x"):
    with so.scheduler_iteration(name):
        raise ValueError("boom")  # swallow by design


# ── scheduler_iteration → notify 觸發節流 ────────────────────────────────────


def test_third_consecutive_failure_triggers_notify_once(monkeypatch):
    """連續第 3 次失敗觸發一次 LINE 通知；第 4、5 次不重複。"""
    spy = MagicMock()
    monkeypatch.setattr(ops_alert, "notify_scheduler_failure", spy)

    _fail_once()
    _fail_once()
    assert spy.call_count == 0, "未達 threshold 不應通知"
    _fail_once()
    assert spy.call_count == 1, "連續第 3 次失敗應觸發一次 LINE 通知"
    kwargs = spy.call_args.kwargs
    assert kwargs["scheduler_name"] == "sched_x"
    assert kwargs["consecutive_failures"] == 3

    _fail_once()
    _fail_once()
    assert spy.call_count == 1, "第 4、5 次失敗不可重複轟炸"


def test_exponential_backoff_realerts_at_9(monkeypatch):
    """跨過 threshold 後持續失敗，於 9 次（3 的冪次）再告警一次。"""
    spy = MagicMock()
    monkeypatch.setattr(ops_alert, "notify_scheduler_failure", spy)

    for _ in range(9):
        _fail_once()
    assert spy.call_count == 2, "3 與 9 次各告警一次（指數退避）"
    assert spy.call_args.kwargs["consecutive_failures"] == 9


def test_success_resets_alert_cycle(monkeypatch):
    """成功 iteration 重置連續失敗計數，下一串失敗重新於第 3 次告警。"""
    spy = MagicMock()
    monkeypatch.setattr(ops_alert, "notify_scheduler_failure", spy)

    for _ in range(3):
        _fail_once()
    assert spy.call_count == 1

    with so.scheduler_iteration("sched_x"):
        pass  # 成功 → reset

    for _ in range(3):
        _fail_once()
    assert spy.call_count == 2, "reset 後新一串失敗應在第 3 次再告警"


def test_notify_exception_does_not_break_scheduler(monkeypatch):
    """notify 自身炸掉不可讓 scheduler_iteration propagate exception。"""
    monkeypatch.setattr(
        ops_alert,
        "notify_scheduler_failure",
        MagicMock(side_effect=RuntimeError("LINE down")),
    )
    for _ in range(3):
        _fail_once()  # 不應 raise


# ── notify_scheduler_failure 本體（LINE 發送點）──────────────────────────────


def test_notify_pushes_to_line_group(_restore_group_id):
    settings.ops_alert.line_group_id = "Cabcdef"
    line = MagicMock()
    ops_alert.init_ops_alert_service(line)

    ops_alert.notify_scheduler_failure(
        scheduler_name="pii_retention",
        error=ValueError("db down"),
        consecutive_failures=3,
    )
    line.push_text_to_group.assert_called_once()
    group_id, text = line.push_text_to_group.call_args.args
    assert group_id == "Cabcdef"
    assert "pii_retention" in text
    assert "3" in text
    assert "db down" in text


def test_notify_no_group_id_skips_silently(caplog, _restore_group_id):
    settings.ops_alert.line_group_id = None
    line = MagicMock()
    ops_alert.init_ops_alert_service(line)
    with caplog.at_level("WARNING"):
        ops_alert.notify_scheduler_failure(
            scheduler_name="s", error=ValueError("x"), consecutive_failures=3
        )
    line.push_text_to_group.assert_not_called()
    assert any("OPS_ALERT_LINE_GROUP_ID 未設" in r.message for r in caplog.records)


def test_notify_push_exception_swallowed(caplog, _restore_group_id):
    settings.ops_alert.line_group_id = "Cabcdef"
    line = MagicMock()
    line.push_text_to_group.side_effect = RuntimeError("LINE API down")
    ops_alert.init_ops_alert_service(line)
    with caplog.at_level("ERROR"):
        ops_alert.notify_scheduler_failure(
            scheduler_name="s", error=ValueError("x"), consecutive_failures=3
        )
    assert any("push 失敗" in r.message for r in caplog.records)
