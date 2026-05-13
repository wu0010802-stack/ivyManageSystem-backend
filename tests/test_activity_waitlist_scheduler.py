"""tests/test_activity_waitlist_scheduler.py — 候補名單排程器測試"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_scheduler_disabled_by_default(monkeypatch):
    """無 env 變數時 scheduler 應停用。"""
    monkeypatch.delenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", raising=False)
    # 強制 reimport 以重讀 env
    import importlib
    from services import activity_waitlist_scheduler

    importlib.reload(activity_waitlist_scheduler)

    assert activity_waitlist_scheduler.scheduler_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_scheduler_enabled_via_env(monkeypatch, value):
    """環境變數正確值應啟用。"""
    monkeypatch.setenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", value)
    from services import activity_waitlist_scheduler

    assert activity_waitlist_scheduler.scheduler_enabled() is True


def test_check_and_sweep_once_returns_dict(monkeypatch):
    """check_and_sweep_once 應回傳 dict（含 expired / reminded / final_reminded）。"""
    from services import activity_waitlist_scheduler

    captured = {}

    def fake_sweep(session):
        captured["called"] = True
        return {"expired": 0, "reminded": 0, "final_reminded": 0}

    class FakeSvc:
        @staticmethod
        def sweep_expired_pending_promotions(session):
            return fake_sweep(session)

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler._get_activity_service",
        lambda: FakeSvc(),
    )

    from contextlib import contextmanager

    @contextmanager
    def fake_session_scope():
        yield None

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.session_scope", fake_session_scope
    )

    result = activity_waitlist_scheduler.check_and_sweep_once()
    assert captured["called"] is True
    assert result == {"expired": 0, "reminded": 0, "final_reminded": 0}


def test_check_and_sweep_once_idempotent(monkeypatch):
    """連跑兩次：不會因為連跑而拋例外（sweep 本身 idempotent 已由其他測試覆蓋）。"""
    from services import activity_waitlist_scheduler

    class FakeSvc:
        @staticmethod
        def sweep_expired_pending_promotions(session):
            return {"expired": 0, "reminded": 0, "final_reminded": 0}

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler._get_activity_service",
        lambda: FakeSvc(),
    )

    from contextlib import contextmanager

    @contextmanager
    def fake_session_scope():
        yield None

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.session_scope", fake_session_scope
    )

    activity_waitlist_scheduler.check_and_sweep_once()
    activity_waitlist_scheduler.check_and_sweep_once()  # 不應拋
