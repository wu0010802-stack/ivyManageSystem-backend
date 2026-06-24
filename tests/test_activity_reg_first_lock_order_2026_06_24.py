"""tests/test_activity_reg_first_lock_order_2026_06_24.py

P3（2026-06-24 才藝模組稽核）：reg-first 群與 course-first 群的 ABBA 鎖序反轉。

canonical 鎖序協議為「advisory → ActivityCourse → ActivityRegistration →
RegistrationCourse」（course-first），confirm/decline/promote/_auto_promote/
withdraw/add_course/public_update 皆已對齊。但下列 reg-first 路徑先持
ActivityRegistration（或過期 RegistrationCourse）列鎖，才在 _auto_promote_first_waitlist
內取 ActivityCourse 鎖，與 course-first 群構成 ABBA：

- delete_registration（services/activity_service.py）
- reject_registration（api/activity/registrations_pending.py）
- sweep_expired_pending_promotions（services/activity_service.py）
- sync_registrations_on_student_deactivate（services/activity_student_sync.py）

修法：上述路徑在持有 reg/RC 列鎖前，先以 order_by(ActivityCourse.id) 對佔位課程
取列鎖（advisory 之後、reg 之前），使全系統鎖序一致、消除 ABBA。

測法沿用 test_activity_public_update_lock_order_2026_06_24.py：spy
sqlalchemy Query.with_for_update 的呼叫順序，斷言 ActivityCourse 先於
ActivityRegistration / RegistrationCourse 被鎖。SQLite 下 with_for_update 為 no-op
但仍會被呼叫，故可驗證鎖『取得順序』。
"""

import os
import sys
from datetime import date, timedelta

import pytest
import sqlalchemy.orm
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
)
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def sf(tmp_path):
    db_path = tmp_path / "reg-first-lock-order.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    yield session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _spy_locks(monkeypatch):
    """安裝 with_for_update spy，回傳記錄被鎖實體順序的 list。"""
    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)
    return recorded


def _assert_course_before(recorded, other_entity):
    locks = [e for e in recorded if e in (ActivityCourse, other_entity)]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert other_entity in locks, f"未鎖 {other_entity.__name__}；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(other_entity), (
        f"鎖序錯誤：應先鎖 ActivityCourse 再鎖 {other_entity.__name__}，"
        f"實際 {[e.__name__ for e in locks]}"
    )


def _make_course(session, name="圍棋", capacity=30, waitlist=True):
    sy, sem = _term()
    c = ActivityCourse(
        name=name,
        price=1000,
        capacity=capacity,
        allow_waitlist=waitlist,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(c)
    session.flush()
    return c


def _make_reg(
    session,
    course,
    *,
    status="enrolled",
    pending=False,
    student_id=None,
    name="王小明",
    phone="0912345678",
):
    sy, sem = _term()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="海豚班",
        parent_phone=phone,
        paid_amount=0,
        is_paid=False,
        is_active=True,
        pending_review=pending,
        match_status="pending" if pending else "matched",
        student_id=student_id,
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status=status,
            price_snapshot=1000,
        )
    )
    session.commit()
    return reg.id


# ── P3-2：delete_registration ──────────────────────────────────────────────


def test_delete_registration_locks_course_before_registration(sf, monkeypatch):
    from services.activity_service import activity_service

    with sf() as s:
        course = _make_course(s)
        reg_id = _make_reg(s, course)

    with sf() as s:
        recorded = _spy_locks(monkeypatch)
        activity_service.delete_registration(s, reg_id, operator="admin")
        s.commit()

    _assert_course_before(recorded, ActivityRegistration)


def test_delete_registration_still_soft_deletes_and_promotes(sf):
    """反回歸：重排鎖序後刪除仍軟刪 reg 並遞補候補第一位。"""
    from services.activity_service import activity_service

    with sf() as s:
        course = _make_course(s, capacity=1)
        enrolled_id = _make_reg(s, course, status="enrolled")
        waiter_id = _make_reg(
            s, course, status="waitlist", name="李小華", phone="0922333444"
        )

    with sf() as s:
        activity_service.delete_registration(s, enrolled_id, operator="admin")
        s.commit()

    with sf() as s:
        gone = s.query(ActivityRegistration).filter_by(id=enrolled_id).one()
        assert gone.is_active is False
        waiter_rc = (
            s.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == waiter_id)
            .one()
        )
        assert waiter_rc.status == "promoted_pending", "候補應遞補為 promoted_pending"


# ── P3-2：reject_registration ──────────────────────────────────────────────


def test_reject_registration_locks_course_before_registration(sf, monkeypatch):
    from api.activity.registrations_pending import reject_registration
    from schemas.activity_admin import RegistrationRejectRequest

    with sf() as s:
        course = _make_course(s)
        reg_id = _make_reg(s, course, status="enrolled", pending=True)

    recorded = _spy_locks(monkeypatch)
    reject_registration(
        reg_id,
        RegistrationRejectRequest(reason="資料不符，校外生"),
        current_user={"username": "admin", "permission_names": ["*"]},
    )

    _assert_course_before(recorded, ActivityRegistration)


def test_reject_registration_still_rejects_and_promotes(sf):
    """反回歸：重排鎖序後拒絕仍軟刪 + 標 rejected + 遞補候補。"""
    from api.activity.registrations_pending import reject_registration
    from schemas.activity_admin import RegistrationRejectRequest

    with sf() as s:
        course = _make_course(s, capacity=1)
        pending_id = _make_reg(s, course, status="enrolled", pending=True)
        waiter_id = _make_reg(
            s, course, status="waitlist", name="李小華", phone="0922333444"
        )

    reject_registration(
        pending_id,
        RegistrationRejectRequest(reason="資料不符，校外生"),
        current_user={"username": "admin", "permission_names": ["*"]},
    )

    with sf() as s:
        rejected = s.query(ActivityRegistration).filter_by(id=pending_id).one()
        assert rejected.is_active is False
        assert rejected.match_status == "rejected"
        waiter_rc = (
            s.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == waiter_id)
            .one()
        )
        assert waiter_rc.status == "promoted_pending"


# ── P3-3：sweep_expired_pending_promotions ─────────────────────────────────


def test_sweep_locks_course_before_registration_course(sf, monkeypatch):
    from services.activity_service import activity_service

    with sf() as s:
        course = _make_course(s, capacity=1)
        # 一筆過期的 promoted_pending（待 sweep 清除 + 遞補）
        sy, sem = _term()
        reg = ActivityRegistration(
            student_name="過期生",
            birthday="2020-01-01",
            class_name="海豚班",
            parent_phone="0912345678",
            paid_amount=0,
            is_paid=False,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="promoted_pending",
                price_snapshot=1000,
                confirm_deadline=now_taipei_naive() - timedelta(hours=1),
            )
        )
        s.commit()

    with sf() as s:
        recorded = _spy_locks(monkeypatch)
        result = activity_service.sweep_expired_pending_promotions(s)
        s.commit()
        assert result["expired"] == 1

    _assert_course_before(recorded, RegistrationCourse)


# ── P3-4：sync_registrations_on_student_deactivate ─────────────────────────


def test_sync_deactivate_locks_course_before_registration(sf, monkeypatch):
    from services.activity_student_sync import (
        sync_registrations_on_student_deactivate,
    )

    with sf() as s:
        classroom = Classroom(name="海豚班", is_active=True)
        s.add(classroom)
        s.flush()
        student = Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 1, 1),
            classroom_id=classroom.id,
            parent_phone="0912345678",
            is_active=True,
        )
        s.add(student)
        s.flush()
        course = _make_course(s, capacity=1)
        _make_reg(s, course, status="enrolled", student_id=student.id)
        sid = student.id

    with sf() as s:
        recorded = _spy_locks(monkeypatch)
        sync_registrations_on_student_deactivate(s, sid)
        s.commit()

    _assert_course_before(recorded, ActivityRegistration)
