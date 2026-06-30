"""轉成學生時 enrollment_semester 應從 visit.target_semester 寫入。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from models.base import Base
from models.classroom import Student
from models.recruitment import RecruitmentVisit
from services.recruitment_conversion import convert_recruitment_to_student


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


def test_conversion_sets_enrollment_semester(session):
    v = RecruitmentVisit(
        month="114.09",
        child_name="轉化童",
        has_deposit=True,
        target_school_year=115,
        target_semester=1,
    )
    session.add(v)
    session.commit()

    result = convert_recruitment_to_student(session, recruitment_visit_id=v.id)
    session.commit()

    student = session.query(Student).get(result.student_id)
    assert student.enrollment_school_year == 115
    assert student.enrollment_semester == 1
