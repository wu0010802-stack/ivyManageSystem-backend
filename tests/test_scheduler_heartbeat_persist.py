"""scheduler_iteration DB persist 行為驗證。

針對新加的 expected_interval_seconds kwarg：
- 成功 tick UPDATE scheduler_heartbeats row 設 last_success_at / consecutive_failures=0
- 失敗 tick UPDATE last_failure_at / consecutive_failures += 1 / last_error_message
- 未傳 kwarg 仍保持原 in-memory only 行為（不寫 DB；對既有 caller 無破壞性）
- DB 寫失敗時 scheduler loop 不應中斷（swallow）
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from models.scheduler_heartbeat import SchedulerHeartbeat
from utils import scheduler_observability as so


@pytest.fixture(autouse=True)
def _reset_metrics():
    so.reset_for_tests()
    yield
    so.reset_for_tests()


def test_success_persists_to_db_when_kwarg_provided(test_db_session):
    test_db_session.add(
        SchedulerHeartbeat(scheduler_name="sched_a", expected_interval_seconds=300)
    )
    test_db_session.commit()

    with so.scheduler_iteration("sched_a", expected_interval_seconds=300):
        pass

    test_db_session.expire_all()
    row = (
        test_db_session.query(SchedulerHeartbeat)
        .filter_by(scheduler_name="sched_a")
        .one()
    )
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0
    assert row.last_error_message is None


def test_failure_persists_to_db_when_kwarg_provided(test_db_session):
    test_db_session.add(
        SchedulerHeartbeat(scheduler_name="sched_b", expected_interval_seconds=300)
    )
    test_db_session.commit()

    with so.scheduler_iteration("sched_b", expected_interval_seconds=300):
        raise ValueError("boom")  # swallow by design

    test_db_session.expire_all()
    row = (
        test_db_session.query(SchedulerHeartbeat)
        .filter_by(scheduler_name="sched_b")
        .one()
    )
    assert row.last_failure_at is not None
    assert row.consecutive_failures == 1
    assert "boom" in (row.last_error_message or "")


def test_no_kwarg_keeps_in_memory_only(test_db_session):
    """未傳 expected_interval_seconds：不寫 DB，僅更新 in-memory metrics。
    既有 caller 無破壞性。"""
    test_db_session.add(
        SchedulerHeartbeat(scheduler_name="sched_c", expected_interval_seconds=300)
    )
    test_db_session.commit()

    with so.scheduler_iteration("sched_c"):
        pass

    test_db_session.expire_all()
    row = (
        test_db_session.query(SchedulerHeartbeat)
        .filter_by(scheduler_name="sched_c")
        .one()
    )
    # 未寫 DB → last_success_at 仍 NULL
    assert row.last_success_at is None

    # in-memory 仍有更新
    snap = so.get_metrics_snapshot()
    assert snap["sched_c"].last_success_at is not None


def test_db_persist_failure_swallowed(test_db_session):
    """DB UPDATE 失敗時 scheduler loop 不中斷（持續觀測 in-memory）。"""
    with patch(
        "utils.scheduler_observability._persist_heartbeat",
        side_effect=Exception("db down"),
    ):
        # 不應 raise — _persist_heartbeat 內部 try/except，但這裡用 mock 直接拋
        # scheduler_iteration 在 success path call 完 _persist_heartbeat 才 return
        # mock 的 raise 會被 scheduler_iteration 內層 try/except 攔下
        try:
            with so.scheduler_iteration("sched_d", expected_interval_seconds=300):
                pass
        except Exception:
            pytest.fail("scheduler_iteration leaked exception from DB persist")

    # in-memory 仍正常更新
    snap = so.get_metrics_snapshot()
    assert snap["sched_d"].last_success_at is not None


def test_upsert_row_if_missing(test_db_session):
    """heartbeat row 不存在時自動建立（防 seed 漏 / 新 scheduler 啟動）。"""
    with so.scheduler_iteration("sched_e_new", expected_interval_seconds=300):
        pass

    test_db_session.expire_all()
    row = (
        test_db_session.query(SchedulerHeartbeat)
        .filter_by(scheduler_name="sched_e_new")
        .one_or_none()
    )
    assert row is not None
    assert row.expected_interval_seconds == 300
    assert row.last_success_at is not None
