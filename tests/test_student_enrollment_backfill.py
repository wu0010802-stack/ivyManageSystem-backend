import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401
from models.classroom import Student, Classroom, ClassGrade
from services.student_numbering import backfill_enrollment_numbers


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


def test_backfill_parses_year_and_reseqs_per_year(session):
    session.add(Student(id=1, student_id="114-A-01", name="甲"))
    session.add(Student(id=2, student_id="114-B-01", name="乙"))
    session.add(Student(id=3, student_id="115-A-03", name="丙"))
    session.flush()

    backfill_enrollment_numbers(session)
    session.flush()

    s1 = session.get(Student, 1)
    s2 = session.get(Student, 2)
    s3 = session.get(Student, 3)
    assert s1.enrollment_school_year == 114 and s2.enrollment_school_year == 114
    assert {s1.enrollment_seq, s2.enrollment_seq} == {1, 2}
    assert s3.enrollment_school_year == 115 and s3.enrollment_seq == 1


def test_backfill_unparseable_uses_enrollment_date_then_idempotent(session):
    from datetime import date

    session.add(
        Student(
            id=1, student_id="LEGACYX", name="無前綴", enrollment_date=date(2025, 9, 1)
        )
    )  # 2025-09 → 114 學年
    session.flush()
    backfill_enrollment_numbers(session)
    session.flush()
    s1 = session.get(Student, 1)
    assert s1.enrollment_school_year == 114
    assert s1.enrollment_seq == 1
    backfill_enrollment_numbers(session)
    session.flush()
    assert session.get(Student, 1).enrollment_seq == 1
