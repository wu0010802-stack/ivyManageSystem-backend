"""sync_registrations_on_student_deactivate SAVEPOINT 回歸測試。

當批次軟刪過程中某筆失敗時：
- 不應汙染 SQLAlchemy session（後續筆仍可寫入）
- 失敗那筆的 is_active 必須維持原值（SAVEPOINT rollback）
- 其他筆正常完成軟刪
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models.database import Base
    from models.academic_term import (
        AcademicTerm,
    )  # 註冊到 Base.metadata 以建 academic_terms 表  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session
    finally:
        session.close()


def _seed_three_regs(session):
    from models.database import ActivityRegistration, Classroom, Student

    classroom = Classroom(name="班A", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S100",
        name="王小明",
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    regs = []
    for i in range(3):
        r = ActivityRegistration(
            student_name=f"王小明{i}",
            class_name="班A",
            classroom_id=classroom.id,
            school_year=sy,
            semester=sem,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(r)
        regs.append(r)
    session.commit()
    return student.id, [r.id for r in regs]


class TestPartialFailureSavepoint:
    def test_failure_on_one_reg_does_not_corrupt_others(
        self, monkeypatch, sqlite_session
    ):
        from services import activity_student_sync as ass
        from models.database import ActivityRegistration

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_three_regs(session)
        failing_id = reg_ids[1]

        original = ass._soft_delete_single_registration

        def patched(session, reg, **kwargs):
            if reg.id == failing_id:
                raise RuntimeError("人為失敗")
            return original(session, reg, **kwargs)

        monkeypatch.setattr(ass, "_soft_delete_single_registration", patched)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        # 兩筆成功軟刪
        assert deleted == 2, f"預期 2 筆成功，實際 {deleted}"

        # 重撈確認狀態
        session.expire_all()
        statuses = {
            r.id: r.is_active
            for r in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(reg_ids))
            .all()
        }
        assert statuses[reg_ids[0]] is False
        assert (
            statuses[failing_id] is True
        ), "SAVEPOINT 未生效：失敗那筆的 is_active 應該維持 True"
        assert statuses[reg_ids[2]] is False

    def test_all_success_returns_full_count(self, sqlite_session):
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        student_id, _ = _seed_three_regs(session)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)
        assert deleted == 3


def _seed_one_student_with_course_reg(session, student_name, course_id, status):
    """為某學生建一筆當學期啟用報名，並掛一筆指定狀態的 RegistrationCourse。

    回傳 (student_id, registration_id, registration_course_id)。
    """
    from models.database import (
        ActivityRegistration,
        Classroom,
        RegistrationCourse,
        Student,
    )
    from utils.academic import resolve_current_academic_term

    classroom = session.query(Classroom).filter(Classroom.name == "班A").first()
    if classroom is None:
        classroom = Classroom(name="班A", is_active=True)
        session.add(classroom)
        session.flush()

    student = Student(
        student_id=f"S-{student_name}",
        name=student_name,
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    sy, sem = resolve_current_academic_term()
    reg = ActivityRegistration(
        student_name=student_name,
        class_name="班A",
        classroom_id=classroom.id,
        school_year=sy,
        semester=sem,
        student_id=student.id,
        is_active=True,
        paid_amount=0,
        match_status="matched",
        pending_review=False,
    )
    session.add(reg)
    session.flush()

    rc = RegistrationCourse(
        registration_id=reg.id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return student.id, reg.id, rc.id


class TestDeactivateTriggersWaitlistPromotion:
    """學生離園/退學軟刪報名後，釋出的名額應自動遞補候補。

    對比 delete_registration：軟刪佔位報名後須對每門課呼叫
    _auto_promote_first_waitlist，否則名額空出但候補卡死。
    """

    def test_deactivate_enrolled_promotes_waitlist(self, sqlite_session):
        from models.database import ActivityCourse, RegistrationCourse
        from services import activity_student_sync as ass

        _engine, session = sqlite_session

        # 一門 capacity=1 課程
        course = ActivityCourse(
            name="美術", price=1000, capacity=1, allow_waitlist=True
        )
        session.add(course)
        session.flush()

        # student_a enrolled（佔位）、student_b waitlist（候補）
        student_a_id, _reg_a_id, _rc_a_id = _seed_one_student_with_course_reg(
            session, "甲生", course.id, "enrolled"
        )
        _student_b_id, _reg_b_id, rc_b_id = _seed_one_student_with_course_reg(
            session, "乙生", course.id, "waitlist"
        )
        session.commit()

        # 甲生離園 → 軟刪其報名，應遞補乙生候補
        ass.sync_registrations_on_student_deactivate(session, student_a_id)
        session.commit()

        session.expire_all()
        rc_b = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.id == rc_b_id)
            .first()
        )
        assert (
            rc_b.status == "promoted_pending"
        ), "甲生離園軟刪後，名額應自動遞補乙生候補（修前仍為 waitlist）"
        assert rc_b.promoted_at is not None
        assert rc_b.confirm_deadline is not None
