"""tests/test_student_enrollment.py — student_enrollment 測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student  # noqa: F401 metadata
from services.student_enrollment import (
    classroom_student_count_map,
    count_students_active_on,
    student_active_on_filter,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _make_classroom(s, name="小班"):
    c = Classroom(name=name, is_active=True)
    s.add(c)
    s.flush()
    return c


def _make_student(
    s, *, student_id, name="X", classroom_id=None, enroll=None, grad=None
):
    st = Student(
        student_id=student_id,
        name=name,
        classroom_id=classroom_id,
        enrollment_date=enroll,
        graduation_date=grad,
    )
    s.add(st)
    s.flush()
    return st


class TestStudentActiveOnFilter:
    def test_returns_sqlalchemy_clause(self):
        # 純函式：只驗證回傳可被 SQLAlchemy filter 接受（不爆例外）
        clause = student_active_on_filter(date(2026, 5, 1))
        assert clause is not None
        assert hasattr(clause, "compile")


class TestCountStudentsActiveOn:
    def test_counts_only_active_in_range(self, session):
        # 未畢業且已入學
        _make_student(session, student_id="S001", enroll=date(2025, 8, 1), grad=None)
        # 已畢業
        _make_student(
            session,
            student_id="S002",
            enroll=date(2024, 8, 1),
            grad=date(2026, 6, 30),
        )
        # 還沒入學
        _make_student(
            session,
            student_id="S003",
            enroll=date(2027, 8, 1),
            grad=None,
        )
        assert count_students_active_on(session, date(2026, 5, 1)) == 2

    def test_null_dates_treated_as_active(self, session):
        # enrollment_date/graduation_date 為 NULL 都視為在籍
        _make_student(session, student_id="S100", enroll=None, grad=None)
        assert count_students_active_on(session, date(2026, 5, 1)) == 1

    def test_filter_by_classroom(self, session):
        c1 = _make_classroom(session, "小一")
        c2 = _make_classroom(session, "小二")
        _make_student(
            session, student_id="A1", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="A2", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="B1", classroom_id=c2.id, enroll=date(2025, 8, 1)
        )
        assert (
            count_students_active_on(session, date(2026, 5, 1), classroom_id=c1.id) == 2
        )
        assert (
            count_students_active_on(session, date(2026, 5, 1), classroom_id=c2.id) == 1
        )

    def test_empty_db_returns_zero(self, session):
        assert count_students_active_on(session, date(2026, 5, 1)) == 0

    def test_boundary_dates_inclusive(self, session):
        # enrollment_date == target_date 應視為在籍
        _make_student(
            session,
            student_id="S200",
            enroll=date(2026, 5, 1),
            grad=date(2026, 5, 1),
        )
        assert count_students_active_on(session, date(2026, 5, 1)) == 1


class TestClassroomStudentCountMap:
    def test_groups_by_classroom_id(self, session):
        c1 = _make_classroom(session, "小一")
        c2 = _make_classroom(session, "小二")
        _make_student(
            session, student_id="A1", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="A2", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="A3", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="B1", classroom_id=c2.id, enroll=date(2025, 8, 1)
        )
        result = classroom_student_count_map(session, date(2026, 5, 1))
        assert result == {c1.id: 3, c2.id: 1}

    def test_excludes_null_classroom_id(self, session):
        c1 = _make_classroom(session, "小一")
        _make_student(
            session, student_id="A1", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        _make_student(
            session, student_id="N1", classroom_id=None, enroll=date(2025, 8, 1)
        )
        result = classroom_student_count_map(session, date(2026, 5, 1))
        assert result == {c1.id: 1}
        assert None not in result

    def test_only_counts_active_students(self, session):
        c1 = _make_classroom(session, "小一")
        # active
        _make_student(
            session, student_id="A1", classroom_id=c1.id, enroll=date(2025, 8, 1)
        )
        # graduated already
        _make_student(
            session,
            student_id="G1",
            classroom_id=c1.id,
            enroll=date(2024, 8, 1),
            grad=date(2026, 1, 1),
        )
        result = classroom_student_count_map(session, date(2026, 5, 1))
        assert result == {c1.id: 1}

    def test_empty_db_returns_empty_dict(self, session):
        result = classroom_student_count_map(session, date(2026, 5, 1))
        assert result == {}
