import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student


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


class TestStudentEnrollmentColumns:
    def test_columns_exist_and_nullable(self, session):
        stu = Student(student_id="LEGACY-1", name="舊生")
        session.add(stu)
        session.flush()
        assert stu.enrollment_school_year is None
        assert stu.enrollment_seq is None

    def test_student_id_no_longer_unique(self, session):
        session.add(Student(student_id="115-中-05", name="A"))
        session.add(Student(student_id="115-中-05", name="B"))
        session.flush()  # 不應因 unique 而炸

    def test_enrollment_key_composite_unique(self, session):
        session.add(
            Student(
                student_id="x1", name="A", enrollment_school_year=114, enrollment_seq=1
            )
        )
        session.flush()
        session.add(
            Student(
                student_id="x2", name="B", enrollment_school_year=114, enrollment_seq=1
            )
        )
        with pytest.raises(Exception):
            session.flush()

from models.classroom import Classroom, ClassGrade
from services.student_numbering import (
    grade_char,
    compute_student_display_id,
    next_enrollment_seq,
)


def _grade(session, name, sort_order=1):
    g = ClassGrade(name=name, sort_order=sort_order)
    session.add(g)
    session.flush()
    return g


def _classroom(session, *, school_year, grade=None, name="班", code="A"):
    c = Classroom(
        name=name, school_year=school_year, semester=1,
        grade_id=(grade.id if grade else None), class_code=code,
    )
    session.add(c)
    session.flush()
    return c


class TestGradeChar:
    def test_first_char(self):
        assert grade_char("大班") == "大"
        assert grade_char("幼幼班") == "幼"
    def test_blank(self):
        assert grade_char(None) == ""
        assert grade_char("  ") == ""


class TestComputeDisplayId:
    def test_in_classroom_with_grade(self, session):
        g = _grade(session, "中班")
        c = _classroom(session, school_year=115, grade=g)
        stu = Student(student_id="tmp", name="A", classroom_id=c.id,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "115-中-05"

    def test_classroom_without_grade(self, session):
        c = _classroom(session, school_year=115, grade=None)
        stu = Student(student_id="tmp", name="A", classroom_id=c.id,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "115-05"

    def test_no_classroom_fallback(self, session):
        stu = Student(student_id="tmp", name="A", classroom_id=None,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "114-05"

    def test_seq_none_returns_existing(self, session):
        stu = Student(student_id="LEGACY", name="A", enrollment_seq=None)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "LEGACY"


class TestNextEnrollmentSeq:
    def test_first_is_one(self, session):
        assert next_enrollment_seq(session, 114) == 1

    def test_increments_within_year(self, session):
        session.add(Student(student_id="a", name="A",
                            enrollment_school_year=114, enrollment_seq=1))
        session.add(Student(student_id="b", name="B",
                            enrollment_school_year=114, enrollment_seq=2))
        session.flush()
        assert next_enrollment_seq(session, 114) == 3

    def test_per_year_independent(self, session):
        session.add(Student(student_id="a", name="A",
                            enrollment_school_year=114, enrollment_seq=7))
        session.flush()
        assert next_enrollment_seq(session, 115) == 1


# Ensure all required tables are created for conversion test
import models.recruitment  # noqa: F401 registers RecruitmentVisit/RecruitmentEventLog
import models.student_log  # noqa: F401 registers StudentChangeLog
import models.guardian  # noqa: F401 registers Guardian


def test_conversion_allocates_enrollment_seq(session, monkeypatch):
    from models.recruitment import RecruitmentVisit
    from services import recruitment_conversion as rc
    monkeypatch.setattr(rc, "resolve_current_academic_term", lambda *a, **k: (114, 1))

    g = _grade(session, "小班")
    c = _classroom(session, school_year=114, grade=g)
    visit = RecruitmentVisit(child_name="小明", month="114.09")
    session.add(visit); session.flush()

    result = rc.convert_recruitment_to_student(
        session, recruitment_visit_id=visit.id, classroom_id=c.id,
    )
    stu = session.get(Student, result.student_id)
    assert stu.enrollment_school_year == 114
    assert stu.enrollment_seq == 1
    assert stu.student_id == "114-小-01"
