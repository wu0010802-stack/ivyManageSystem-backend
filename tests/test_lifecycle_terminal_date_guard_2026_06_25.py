"""資料品質（bug-hunt 2026-06-25）：lifecycle transition() 終態日期守衛。

問題：POST /students/{id}/lifecycle → transition() 接受任意 effective_date，直接寫成
graduation_date / withdrawal_date，不檢查是否早於入學日。舊 graduate_student / bulk_graduate
端點皆有「離園日不可早於入學日」守衛（api/students.py:1196），同業務語意兩條路徑不一致；
無 DB CHECK 兜底。離校日早於入學日會污染在籍報表與年資/年終日期基準
（mark_salary_stale_for_enrollment_event 以該日為事件日標記重算）。

修法：在 transition() 對轉入 graduated/transferred/withdrawn 補一致守衛
（effective_date 不可早於 student.enrollment_date），raise LifecycleTransitionError
（端點轉 400，與 graduate_student 一致）。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from services.student_lifecycle import LifecycleTransitionError, transition


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def classroom(session):
    c = Classroom(name="測試班", school_year=114, semester=1)
    session.add(c)
    session.flush()
    return c


def _make_student(session, classroom):
    s = Student(
        student_id="G001",
        name="測試生",
        classroom_id=classroom.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
        is_active=True,
        enrollment_date=date(2026, 2, 1),
    )
    session.add(s)
    session.flush()
    return s


_TERMINAL = [LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN]


@pytest.mark.parametrize("to_status", _TERMINAL)
def test_terminal_date_before_enrollment_rejected(session, classroom, to_status):
    student = _make_student(session, classroom)  # enrollment_date=2026-02-01
    with pytest.raises(LifecycleTransitionError):
        transition(
            session,
            student,
            to_status=to_status,
            effective_date=date(2026, 1, 15),  # 早於入學日
        )


@pytest.mark.parametrize("to_status", _TERMINAL)
def test_terminal_date_on_or_after_enrollment_ok(session, classroom, to_status):
    student = _make_student(session, classroom)
    # 邊界：等於入學日（合法，對齊 graduate_student 的 < 守衛）
    transition(
        session,
        student,
        to_status=to_status,
        effective_date=date(2026, 2, 1),
    )
    session.commit()
    assert student.lifecycle_status == to_status


def test_terminal_no_enrollment_date_not_blocked(session, classroom):
    """enrollment_date 為 None 時不應誤擋（對齊 graduate_student 的 `student.enrollment_date and`）。"""
    student = _make_student(session, classroom)
    student.enrollment_date = None
    session.flush()
    transition(
        session,
        student,
        to_status=LIFECYCLE_WITHDRAWN,
        effective_date=date(2020, 1, 1),
    )
    session.commit()
    assert student.lifecycle_status == LIFECYCLE_WITHDRAWN
