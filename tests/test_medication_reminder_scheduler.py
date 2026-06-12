"""medication_reminder_scheduler 安全網測試（兒童用藥安全；audit P1 #7）。

核心安全邏輯 `_active_orders_query`：今日 medication orders 必須**排除已核准請假
（覆蓋今日）的學生**——請假缺席的孩子若仍被列入餵藥提醒，會誤導老師/家長。
本檔鎖定此行為，避免日後 regression。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    Base,
    Classroom,
    Student,
    User,
)
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from services import medication_reminder_scheduler as sched

TODAY = date(2026, 5, 20)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    s = SessionFactory()
    # 共用前置：1 班、1 申請人 user、2 學生
    s.add(Classroom(id=1, name="兔兔班", is_active=True))
    s.add(
        User(
            id=1,
            username="teacher",
            password_hash="x",
            role="teacher",
            is_active=True,
            token_version=0,
        )
    )
    s.add(
        Student(
            id=1,
            student_id="S001",
            name="小明",
            classroom_id=1,
            lifecycle_status="active",
        )
    )
    s.add(
        Student(
            id=2,
            student_id="S002",
            name="小華",
            classroom_id=1,
            lifecycle_status="active",
        )
    )
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _add_order(session, *, student_id: int, order_date: date = TODAY):
    session.add(
        StudentMedicationOrder(
            student_id=student_id,
            order_date=order_date,
            medication_name="退燒藥",
            dose="1 包",
            time_slots=["12:00"],
            source="teacher",
        )
    )
    session.commit()


def _add_leave(
    session, *, student_id: int, start: date, end: date, status: str = "approved"
):
    session.add(
        StudentLeaveRequest(
            student_id=student_id,
            applicant_user_id=1,
            leave_type="病假",
            start_date=start,
            end_date=end,
            status=status,
        )
    )
    session.commit()


def _ids(query):
    return {o.student_id for o in query.all()}


class TestActiveOrdersQuery:
    def test_today_order_without_leave_is_included(self, session):
        _add_order(session, student_id=1)
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_order_for_other_day_excluded(self, session):
        _add_order(session, student_id=1, order_date=TODAY - timedelta(days=1))
        assert _ids(sched._active_orders_query(session, TODAY)) == set()

    def test_student_on_approved_leave_today_is_excluded(self, session):
        """兒童安全關鍵：請假缺席學生不該被列入今日餵藥提醒。"""
        _add_order(session, student_id=1)
        _add_order(session, student_id=2)
        _add_leave(session, student_id=2, start=TODAY, end=TODAY)
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_leave_boundary_end_yesterday_included(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY - timedelta(days=3),
            end=TODAY - timedelta(days=1),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_leave_boundary_start_tomorrow_included(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY + timedelta(days=1),
            end=TODAY + timedelta(days=3),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_multi_day_leave_covering_today_excluded(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY - timedelta(days=1),
            end=TODAY + timedelta(days=1),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == set()

    def test_non_approved_leave_does_not_exclude(self, session):
        """只有 approved 請假才排除；pending 請假不影響（避免漏餵藥）。"""
        _add_order(session, student_id=1)
        _add_leave(session, student_id=1, start=TODAY, end=TODAY, status="pending")
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_student_in_terminal_lifecycle_is_excluded(self, session):
        """終態學生（已畢業/退學/轉出）已永久離校，今日 order 不該列入提醒。

        對稱於請假排除：scheduler 已用 leave 子查詢證明它在意「學生今天是否在校」，
        終態是更強的「永久不在校」訊號。
        """
        _add_order(session, student_id=1)
        _add_order(session, student_id=2)
        session.query(Student).filter_by(id=2).update({"lifecycle_status": "withdrawn"})
        session.commit()
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}


class TestCountAndRun:
    def test_count_today_medication_orders(self, session, monkeypatch):
        from contextlib import contextmanager

        _add_order(session, student_id=1)
        _add_order(session, student_id=2)
        _add_leave(session, student_id=2, start=TODAY, end=TODAY)

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(sched, "session_scope", _scope)
        # student 2 請假被排除 → 只剩 1
        assert sched.count_today_medication_orders(TODAY) == 1

    def test_run_medication_reminder_returns_order_count(self, session, monkeypatch):
        from contextlib import contextmanager

        _add_order(session, student_id=1)

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(sched, "session_scope", _scope)
        result = sched.run_medication_reminder(effective_date=TODAY)
        assert result["date"] == TODAY.isoformat()
        assert result["order_count"] == 1
        assert not result.get("skipped")


class TestWatermarkRestartDedup:
    """重啟漏發/重複發修補：watermark 持久化。

    原 in-memory last_run_date：同日重啟會重發、跨日停機後游標遺失。
    比照 announcement_publish_scheduler 接 utils/scheduler_watermark：
    - 同日第二次啟動（watermark = 今日）→ 不重發
    - 重啟後當日目標時刻已過且 watermark 落後（< 今日）→ 補發一次
    """

    def _patch_scope(self, session, monkeypatch):
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(sched, "session_scope", _scope)

    def _run_loop_briefly(self, monkeypatch):
        """以 0.01s 巡檢間隔跑 loop 約 0.1s 後停（不依賴 pytest-asyncio）。"""
        import asyncio

        import utils.scheduler_observability as so

        monkeypatch.setattr(sched, "CHECK_INTERVAL_SECONDS", 0.01)
        # heartbeat persist 走真 DB session，單元測試 no-op 化
        monkeypatch.setattr(so, "_persist_heartbeat", lambda **kw: None)

        async def _go():
            stop = asyncio.Event()
            task = asyncio.create_task(sched.medication_reminder_loop(stop))
            await asyncio.sleep(0.1)
            stop.set()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(_go())

    def _fix_now(self, monkeypatch, *, hour=8, minute=0):
        from datetime import datetime, time as dt_time

        monkeypatch.setattr(sched, "REMINDER_HOUR", 7)
        monkeypatch.setattr(sched, "REMINDER_MINUTE", 30)
        fixed = datetime.combine(TODAY, dt_time(hour, minute), tzinfo=sched.TAIPEI_TZ)
        monkeypatch.setattr(sched, "_now_taipei", lambda: fixed)

    def test_run_persists_watermark(self, session, monkeypatch):
        """成功跑完一次 → watermark 落 DB，date 部分 = 當日。"""
        from utils.scheduler_watermark import get_watermark

        self._patch_scope(session, monkeypatch)
        _add_order(session, student_id=1)
        sched.run_medication_reminder(effective_date=TODAY)
        ts = get_watermark(session, "medication_reminder")
        assert ts is not None and ts.date() == TODAY

    def test_skipped_run_does_not_persist_watermark(self, session, monkeypatch):
        """advisory lock 沒拿到（他 worker 在跑）→ 不可推進 watermark。"""
        from contextlib import contextmanager

        from utils import advisory_lock
        from utils.scheduler_watermark import get_watermark

        self._patch_scope(session, monkeypatch)

        @contextmanager
        def _no_lock(session, *, scheduler_name, run_key):
            yield False

        monkeypatch.setattr(advisory_lock, "try_scheduler_lock", _no_lock)
        result = sched.run_medication_reminder(effective_date=TODAY)
        assert result.get("skipped") is True
        assert get_watermark(session, "medication_reminder") is None

    def test_loop_same_day_restart_does_not_resend(self, session, monkeypatch):
        """同日第二次啟動：watermark = 今日 → loop 不再觸發（重啟不重發）。"""
        from datetime import datetime, time as dt_time
        from unittest.mock import MagicMock

        from utils.scheduler_watermark import set_watermark

        set_watermark(
            session, "medication_reminder", datetime.combine(TODAY, dt_time(7, 30))
        )
        session.commit()

        self._patch_scope(session, monkeypatch)
        self._fix_now(monkeypatch)  # 08:00 已過 07:30 目標時刻
        spy = MagicMock(return_value={"date": TODAY.isoformat(), "order_count": 0})
        monkeypatch.setattr(sched, "run_medication_reminder", spy)

        self._run_loop_briefly(monkeypatch)
        assert spy.call_count == 0, "同日重啟不可重發提醒"

    def test_loop_stale_watermark_late_start_catches_up_once(
        self, session, monkeypatch
    ):
        """重啟後目標時刻已過且 watermark 落後（昨日）→ 補發恰好一次。"""
        from datetime import datetime, time as dt_time, timedelta
        from unittest.mock import MagicMock

        from utils.scheduler_watermark import set_watermark

        set_watermark(
            session,
            "medication_reminder",
            datetime.combine(TODAY - timedelta(days=1), dt_time(7, 30)),
        )
        session.commit()

        self._patch_scope(session, monkeypatch)
        self._fix_now(monkeypatch)
        spy = MagicMock(return_value={"date": TODAY.isoformat(), "order_count": 0})
        monkeypatch.setattr(sched, "run_medication_reminder", spy)

        self._run_loop_briefly(monkeypatch)
        assert spy.call_count == 1, "watermark 落後應補發一次且僅一次"
        assert spy.call_args.kwargs.get("effective_date") == TODAY

    def test_loop_no_watermark_runs_once_after_target(self, session, monkeypatch):
        """首次啟動（無 watermark）且已過目標時刻 → 跑一次（保留既有保險行為）。"""
        from unittest.mock import MagicMock

        self._patch_scope(session, monkeypatch)
        self._fix_now(monkeypatch)
        spy = MagicMock(return_value={"date": TODAY.isoformat(), "order_count": 0})
        monkeypatch.setattr(sched, "run_medication_reminder", spy)

        self._run_loop_briefly(monkeypatch)
        assert spy.call_count == 1

    def test_loop_before_target_does_not_run(self, session, monkeypatch):
        """未到目標時刻不觸發（不因 watermark 落後就提前跑）。"""
        from datetime import datetime, time as dt_time, timedelta
        from unittest.mock import MagicMock

        from utils.scheduler_watermark import set_watermark

        set_watermark(
            session,
            "medication_reminder",
            datetime.combine(TODAY - timedelta(days=1), dt_time(7, 30)),
        )
        session.commit()

        self._patch_scope(session, monkeypatch)
        self._fix_now(monkeypatch, hour=6, minute=0)  # 06:00 < 07:30
        spy = MagicMock(return_value={"date": TODAY.isoformat(), "order_count": 0})
        monkeypatch.setattr(sched, "run_medication_reminder", spy)

        self._run_loop_briefly(monkeypatch)
        assert spy.call_count == 0
