"""tests/test_recruitment_intake_plan.py — 新生名額規劃模型 + 彙總純函式。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import ClassGrade, Student, LIFECYCLE_ENROLLED
from models.recruitment import RecruitmentVisit, GradeIntakeTarget


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


def test_model_columns_and_target_table(session):
    grade = ClassGrade(name="中班", sort_order=2)
    session.add(grade)
    session.flush()

    v = RecruitmentVisit(
        month="115.03",
        child_name="王小寶",
        has_deposit=True,
        provisional_grade_id=grade.id,
        target_school_year=115,
        target_semester=1,
    )
    session.add(v)

    t = GradeIntakeTarget(
        grade_id=grade.id, school_year=115, semester=1, target_seats=30
    )
    session.add(t)
    session.flush()

    got = session.query(RecruitmentVisit).first()
    assert got.provisional_grade_id == grade.id
    assert got.target_school_year == 115
    assert got.target_semester == 1
    assert session.query(GradeIntakeTarget).first().target_seats == 30
