"""Tests for services.announcement_publish_scheduler.tick."""

from datetime import datetime
from unittest.mock import patch

import pytest

from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Employee,
)
from services.announcement_publish_scheduler import _initial_watermark, tick
from utils.scheduler_watermark import get_watermark, set_watermark
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def admin_emp(test_db_session):
    emp = Employee(employee_id="E_ADMIN", name="admin", is_active=True, base_salary=0)
    test_db_session.add(emp)
    test_db_session.commit()
    return emp


def _mk_ann(session, emp_id, publish_at, has_parent=True):
    a = Announcement(title="T", content="C", created_by=emp_id, publish_at=publish_at)
    session.add(a)
    session.flush()
    if has_parent:
        session.add(AnnouncementParentRecipient(announcement_id=a.id, scope="all"))
        session.flush()
    return a


def test_tick_dispatches_window_only(test_db_session, admin_emp):
    """Only announcements with publish_at in (last_dispatched_at, now] should fire."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    a_in = _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    _mk_ann(
        test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 58, 0)
    )  # before window
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 8, 1, 0))  # future
    _mk_ann(
        test_db_session,
        admin_emp.id,
        datetime(2026, 5, 29, 7, 59, 45),
        has_parent=False,
    )  # no parent
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        count = tick(test_db_session, now=now, last_dispatched_at=last)

    assert count == 1
    assert mock_fire.call_count == 1
    fired_ann = mock_fire.call_args.args[1]
    assert fired_ann.id == a_in.id


def test_tick_idempotent_within_same_window(test_db_session, admin_emp):
    """重跑同 (now, last_dispatched_at) 不重複推播；推進 last 後不再 fire。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        c1 = tick(test_db_session, now=now, last_dispatched_at=last)
        c2 = tick(test_db_session, now=now, last_dispatched_at=now)

    assert c1 == 1
    assert c2 == 0
    assert mock_fire.call_count == 1


# ───────── watermark 持久化（重啟漏推 bug 回歸） ─────────


def test_initial_watermark_uses_persisted_value(test_db_session):
    """重啟後 seed 必須讀回持久化游標，而非用 now()。

    這是漏推 bug 的根因：run loop 啟動把游標初始化成 now()，於是
    (舊游標, now] 窗口內排程的公告永久不會推。修復後 seed 須來自 DB。
    """
    set_watermark(
        test_db_session, "announcement_publish", datetime(2026, 5, 29, 7, 0, 0)
    )
    test_db_session.commit()

    assert _initial_watermark(test_db_session) == datetime(2026, 5, 29, 7, 0, 0)


def test_initial_watermark_falls_back_to_now_when_unset(test_db_session):
    """首次啟動（無持久化游標）fallback now()，避免重推所有歷史排程公告。"""
    before = now_taipei_naive()
    seeded = _initial_watermark(test_db_session)
    after = now_taipei_naive()

    assert before <= seeded <= after


def test_tick_persists_watermark_atomically(test_db_session, admin_emp):
    """tick 推進游標須與 enqueue 同事務落地，重啟後可從 DB 讀回。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    test_db_session.commit()

    with patch("services.announcement_publish_scheduler._fire_announcement_push"):
        tick(test_db_session, now=now, last_dispatched_at=last)

    assert get_watermark(test_db_session, "announcement_publish") == now


def test_tick_advances_watermark_even_with_no_rows(test_db_session):
    """空窗口也要推進游標，否則重啟 seed 用舊游標會重推之後所有公告。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)

    with patch("services.announcement_publish_scheduler._fire_announcement_push"):
        count = tick(test_db_session, now=now, last_dispatched_at=last)

    assert count == 0
    assert get_watermark(test_db_session, "announcement_publish") == now


# ───────── 推播失敗不得吞掉、游標不得越過失敗公告（bug #22 / #23 回歸） ─────────


def test_tick_raises_when_push_fails(test_db_session, admin_emp):
    """單筆推播失敗時 tick 須讓例外冒泡（不靜默吞），以便 scheduler_iteration
    記為失敗並回滾整批重試。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push",
        side_effect=RuntimeError("LINE down"),
    ):
        with pytest.raises(RuntimeError, match="LINE down"):
            tick(test_db_session, now=now, last_dispatched_at=last)


def test_tick_does_not_advance_watermark_on_push_failure(test_db_session, admin_emp):
    """bug #23 回歸：推播失敗時游標不得越過該筆公告，否則該公告永久略過、
    家長永不收到。失敗後 watermark 須維持在 last_dispatched_at（這裡為未設定）。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push",
        side_effect=RuntimeError("LINE down"),
    ):
        with pytest.raises(RuntimeError):
            tick(test_db_session, now=now, last_dispatched_at=last)

    # 失敗回滾後 session 須 rollback 才能讀到回滾後狀態
    test_db_session.rollback()
    # 游標未被推進（仍未設定），下次 tick 會重新涵蓋這筆公告
    assert get_watermark(test_db_session, "announcement_publish") is None


def test_scheduler_iteration_records_failure_when_tick_raises(
    test_db_session, admin_emp
):
    """bug #22 回歸：tick 例外須能傳進 scheduler_iteration 被記為失敗。

    驗證 run loop 不再用內層 try/except 吞掉 tick 例外——直接以
    scheduler_iteration 包住一個會 raise 的 tick，斷言 consecutive_failures 上升。
    """
    from utils import scheduler_observability as obs

    obs.reset_for_tests()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push",
        side_effect=RuntimeError("boom"),
    ):
        with obs.scheduler_iteration("announcement_publish"):
            now = datetime(2026, 5, 29, 8, 0, 0)
            last = datetime(2026, 5, 29, 7, 59, 0)
            _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
            test_db_session.commit()
            tick(test_db_session, now=now, last_dispatched_at=last)

    snap = obs.get_metrics_snapshot()["announcement_publish"]
    assert snap.consecutive_failures == 1
    assert snap.last_success_at is None
