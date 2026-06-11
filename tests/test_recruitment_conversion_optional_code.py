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
    # student_id_code=None 現在走新路徑：配發 enrollment_seq + listener 組學號。
    # classroom 未設 grade_id，listener 組出 "{school_year}-{seq:02d}"
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code=None,
        classroom_id=classroom.id,
    )
    student = session.get(Student, result.student_id)
    assert student.enrollment_seq == 1
    assert student.enrollment_school_year is not None
    # 學號由 listener 自動組出（無年級字時格式為 "{school_year}-{seq:02d}"）
    assert student.student_id == f"{student.enrollment_school_year}-01"


def test_explicit_code_is_now_ignored(session, classroom, visit):
    # student_id_code 已廢棄 / ignored。傳入 "CUSTOM-001" 不影響學號，
    # 學號由 enrollment_seq + listener 組出。
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code="CUSTOM-001",
        classroom_id=classroom.id,
    )
    stu = session.get(Student, result.student_id)
    assert stu.student_id != "CUSTOM-001"
    assert stu.enrollment_seq == 1


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


def test_no_classroom_still_assigns_seq(session, visit):
    # classroom_id=None 允許。
    # 新邏輯不需 classroom；會配發 enrollment_seq 並建立 student。
    # 無 classroom → student_id 為 "{enrollment_school_year}-{seq:02d}"
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code=None,
        classroom_id=None,
    )
    stu = session.get(Student, result.student_id)
    assert stu.enrollment_seq == 1
    assert stu.classroom_id is None


# ── R4-1：students.recruitment_visit_id partial unique index ──


def test_recruitment_visit_id_unique_partial_index(session, classroom, visit):
    """一個 recruitment_visit 最多對應一個 Student（partial unique index 兜底並發
    轉換 TOCTOU）；NULL 允許多筆（未轉換的學生不受限）。"""
    from sqlalchemy.exc import IntegrityError
    from models.classroom import LIFECYCLE_ACTIVE

    s1 = Student(
        student_id="C1",
        name="甲",
        classroom_id=classroom.id,
        recruitment_visit_id=visit.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(s1)
    session.flush()

    s2 = Student(
        student_id="C2",
        name="乙",
        classroom_id=classroom.id,
        recruitment_visit_id=visit.id,  # 同一 visit → 違反 unique
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(s2)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    # NULL recruitment_visit_id 可多筆
    a = Student(
        student_id="N1", name="丙", classroom_id=classroom.id,
        recruitment_visit_id=None, is_active=True, lifecycle_status=LIFECYCLE_ACTIVE,
    )
    b = Student(
        student_id="N2", name="丁", classroom_id=classroom.id,
        recruitment_visit_id=None, is_active=True, lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add_all([a, b])
    session.flush()  # 不應 raise
