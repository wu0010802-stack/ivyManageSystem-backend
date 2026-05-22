"""驗 advance_term_to_active：批量推進 enrolled 學生升 active。

照 test_recruitment_funnel_transitions.py 的 SQLite in-memory pattern。
"""

import os
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 預先載入所有 model 讓 Base.metadata 認得（與其他 funnel test 一致）
import models.student_log  # noqa: F401
import models.fees  # noqa: F401
import models.portfolio  # noqa: F401

from models.base import Base
from models.academic_term import AcademicTerm
from models.classroom import Student, LIFECYCLE_ENROLLED, LIFECYCLE_ACTIVE
from models.recruitment import RecruitmentVisit
from services.recruitment_lifecycle import advance_term_to_active


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
def term_115_1(session):
    t = AcademicTerm(
        school_year=115,
        semester=1,
        start_date=date(2026, 8, 30),
        end_date=date(2027, 1, 31),
    )
    session.add(t)
    session.flush()
    return t


def _make_visit_and_student(session, *, lifecycle, enroll_date=None, sid="115-A-01"):
    v = RecruitmentVisit(
        month="115.03",
        child_name="測試",
        has_deposit=True,
        enrolled=True,
    )
    session.add(v)
    session.flush()
    s = Student(
        student_id=sid,
        name=f"測試-{sid}",
        lifecycle_status=lifecycle,
        recruitment_visit_id=v.id,
        enrollment_date=enroll_date,
        is_active=True,
    )
    session.add(s)
    session.flush()
    return v, s


def test_advances_enrolled_in_window(session, term_115_1):
    """開學前 29 天報到 → 落在 90 天 window → 推進。"""
    _v, s = _make_visit_and_student(
        session,
        lifecycle=LIFECYCLE_ENROLLED,
        enroll_date=date(2026, 8, 1),
        sid="115-A-01",
    )
    summary = advance_term_to_active(session, school_year=115, semester=1)
    session.flush()
    session.refresh(s)
    assert s.lifecycle_status == LIFECYCLE_ACTIVE
    assert summary["advanced"] == 1


def test_skips_already_active(session, term_115_1):
    _v, s = _make_visit_and_student(
        session,
        lifecycle=LIFECYCLE_ACTIVE,
        enroll_date=date(2026, 8, 1),
        sid="115-A-02",
    )
    summary = advance_term_to_active(session, school_year=115, semester=1)
    assert summary["advanced"] == 0


def test_skips_out_of_window(session, term_115_1):
    """開學前 ~241 天 → 超過 90 天 → 不推。"""
    _v, s = _make_visit_and_student(
        session,
        lifecycle=LIFECYCLE_ENROLLED,
        enroll_date=date(2026, 1, 1),
        sid="115-A-03",
    )
    summary = advance_term_to_active(session, school_year=115, semester=1)
    assert summary["advanced"] == 0


def test_skips_null_enrollment_date(session, term_115_1):
    _v, s = _make_visit_and_student(
        session,
        lifecycle=LIFECYCLE_ENROLLED,
        enroll_date=None,
        sid="115-A-04",
    )
    summary = advance_term_to_active(session, school_year=115, semester=1)
    assert summary["advanced"] == 0


def test_term_not_found_returns_zero(session):
    summary = advance_term_to_active(session, school_year=999, semester=1)
    assert summary == {"advanced": 0, "skipped": 0, "failed": 0}
