"""驗 convert_recruitment_to_student 的 student_id_code 自動產生 + funnel event log。

照 test_recruitment_conversion.py 的 SQLite in-memory pattern。
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
from models.classroom import Classroom, Student
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from services.recruitment_conversion import (
    RecruitmentConversionError,
    convert_recruitment_to_student,
)


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
    c = Classroom(name="小班-甲", school_year=114, semester=1, class_code="A")
    session.add(c)
    session.flush()
    return c


@pytest.fixture
def visit(session):
    v = RecruitmentVisit(
        month="115.03",
        child_name="王小寶",
        has_deposit=True,
    )
    session.add(v)
    session.flush()
    return v


def test_auto_generate_student_id_when_omitted(session, classroom, visit):
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code=None,
        classroom_id=classroom.id,
    )
    student = session.get(Student, result.student_id)
    # 學號自動產出，前綴與班代碼一致
    assert student.student_id.startswith(
        f"{classroom.school_year}-{classroom.class_code}-"
    )
    assert student.student_id.endswith("-01")  # 首位


def test_explicit_code_still_works(session, classroom, visit):
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code="CUSTOM-001",
        classroom_id=classroom.id,
    )
    assert session.get(Student, result.student_id).student_id == "CUSTOM-001"


def test_writes_event_log_converted(session, classroom, visit):
    convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code=None,
        classroom_id=classroom.id,
    )
    log = (
        session.query(RecruitmentEventLog)
        .filter_by(
            recruitment_visit_id=visit.id,
            event_type="converted",
        )
        .one()
    )
    assert log.from_stage == "deposited"
    assert log.to_stage == "enrolled"
    assert log.student_id is not None


def test_classroom_required_when_auto_generating(session, visit):
    with pytest.raises(RecruitmentConversionError) as exc:
        convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            student_id_code=None,
            classroom_id=None,
        )
    msg = str(exc.value).lower()
    assert "classroom" in msg or "班" in msg
