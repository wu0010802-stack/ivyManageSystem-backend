"""下學年新生轉換：enrollment_school_year 取自 visit.target_school_year。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def test_uses_target_school_year_when_set(session):
    v = RecruitmentVisit(
        month="115.03",
        child_name="甲",
        has_deposit=True,
        target_school_year=999,
        target_semester=1,  # 用不可能的學年凸顯來源
    )
    session.add(v)
    session.commit()
    result = convert_recruitment_to_student(session, recruitment_visit_id=v.id)
    session.commit()
    student = session.query(Student).get(result.student_id)
    assert student.enrollment_school_year == 999


def test_falls_back_to_current_term_when_no_target(session):
    from utils.academic import resolve_current_academic_term

    cur_year, _ = resolve_current_academic_term()
    v = RecruitmentVisit(month="115.03", child_name="乙", has_deposit=True)
    session.add(v)
    session.commit()
    result = convert_recruitment_to_student(session, recruitment_visit_id=v.id)
    session.commit()
    student = session.query(Student).get(result.student_id)
    assert student.enrollment_school_year == cur_year
