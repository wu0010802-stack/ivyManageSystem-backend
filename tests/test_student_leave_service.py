"""services/student_leave_service.apply_attendance_for_leave / revert_attendance_for_leave 單元測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Classroom,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
)
from services.student_leave_service import (
    apply_attendance_for_leave,
    revert_attendance_for_leave,
)


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "svc.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = Session
    Base.metadata.create_all(engine)
    s = Session()
    yield s
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def _make_family(s):
    user = User(
        username="p", password_hash="!", role="parent", permissions=0, is_active=True
    )
    s.add(user)
    s.flush()
    classroom = Classroom(name="A", is_active=True)
    s.add(classroom)
    s.flush()
    student = Student(
        student_id="S1", name="小明", classroom_id=classroom.id, is_active=True
    )
    s.add(student)
    s.flush()
    return user, student


def test_apply_writes_attendance_with_null_recorded_by(session):
    user, student = _make_family(session)
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="病假",
        start_date=date(2026, 5, 4),  # 週一
        end_date=date(2026, 5, 5),  # 週二
        status="approved",
    )
    session.add(leave)
    session.flush()

    affected = apply_attendance_for_leave(session, leave)
    session.flush()

    assert affected == 2
    rows = session.query(StudentAttendance).filter_by(student_id=student.id).all()
    assert len(rows) == 2
    for r in rows:
        assert r.status == "病假"
        assert r.recorded_by is None
        assert r.remark == f"家長申請#{leave.id}"


def test_apply_preserves_existing_recorded_by_on_conflict(session):
    user, student = _make_family(session)
    teacher = User(
        username="t", password_hash="!", role="teacher", permissions=0, is_active=True
    )
    session.add(teacher)
    session.flush()
    # 教師事先打了一筆考勤
    session.add(
        StudentAttendance(
            student_id=student.id,
            date=date(2026, 5, 4),
            status="到校",
            remark="教師手寫",
            recorded_by=teacher.id,
        )
    )
    session.flush()
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="事假",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 4),
        status="approved",
    )
    session.add(leave)
    session.flush()

    apply_attendance_for_leave(session, leave)
    session.flush()
    rec = session.query(StudentAttendance).filter_by(student_id=student.id).one()
    assert rec.status == "事假"
    assert rec.remark == f"家長申請#{leave.id}"
    assert rec.recorded_by == teacher.id  # 保留


def test_revert_only_removes_own_remark(session):
    user, student = _make_family(session)
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="病假",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 5),
        status="approved",
    )
    session.add(leave)
    session.flush()
    apply_attendance_for_leave(session, leave)
    # 額外塞一筆 leave 範圍外的教師手寫紀錄（不該被清）
    # 5/6（週三）在 leave 範圍 5/4~5/5 之外，不會觸發 UNIQUE 衝突
    session.add(
        StudentAttendance(
            student_id=student.id,
            date=date(2026, 5, 6),
            status="病假",
            remark="教師手寫",
            recorded_by=None,
        )
    )
    session.flush()

    affected = revert_attendance_for_leave(session, leave)
    session.flush()
    assert affected == 2  # 5/4、5/5 兩天都是 leave 寫的
    remaining = session.query(StudentAttendance).filter_by(student_id=student.id).all()
    # 5/6 教師手寫的紀錄應保留
    assert any(r.remark == "教師手寫" for r in remaining)
