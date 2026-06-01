import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401  確保 listener 已註冊
from models.classroom import Student, Classroom, ClassGrade


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


def _grade(session, name):
    g = ClassGrade(name=name, sort_order=1)
    session.add(g)
    session.flush()
    return g


def _classroom(session, school_year, grade):
    c = Classroom(
        name="班",
        school_year=school_year,
        semester=1,
        grade_id=grade.id,
        class_code="A",
    )
    session.add(c)
    session.flush()
    return c


class TestRecomputeListener:
    def test_computed_on_insert(self, session):
        c = _classroom(session, 114, _grade(session, "小班"))
        stu = Student(
            student_id="will-be-overwritten",
            name="A",
            classroom_id=c.id,
            enrollment_school_year=114,
            enrollment_seq=5,
        )
        session.add(stu)
        session.flush()
        assert stu.student_id == "114-小-05"

    def test_recomputed_on_classroom_change(self, session):
        small = _classroom(session, 114, _grade(session, "小班"))
        mid = _classroom(session, 115, _grade(session, "中班"))
        stu = Student(
            student_id="x",
            name="A",
            classroom_id=small.id,
            enrollment_school_year=114,
            enrollment_seq=5,
        )
        session.add(stu)
        session.flush()
        assert stu.student_id == "114-小-05"
        stu.classroom_id = mid.id  # 升年級
        session.flush()
        assert stu.student_id == "115-中-05"  # seq 不變

    def test_seq_none_not_touched(self, session):
        c = _classroom(session, 114, _grade(session, "小班"))
        stu = Student(student_id="LEGACY-1", name="A", classroom_id=c.id)  # 無 seq
        session.add(stu)
        session.flush()
        assert stu.student_id == "LEGACY-1"  # listener 略過
