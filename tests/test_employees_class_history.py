"""tests/test_employees_class_history.py — 員工班級歷程 service + endpoint 測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from models.base import Base
from models.database import (
    Classroom,
    Employee,
    Student,
    User,
)
from models.classroom import ClassGrade
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.employee_class_history import _term_headcounts, build_class_history
from utils.auth import hash_password


@pytest.fixture
def db():
    """SQLite in-memory session（swap 全域 engine）。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    s = session_factory()
    try:
        yield s, session_factory
    finally:
        s.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf
        engine.dispose()


def _mk_classroom(
    s,
    *,
    name,
    school_year,
    semester,
    head=None,
    assistant=None,
    art=None,
    grade_id=None,
):
    c = Classroom(
        name=name,
        school_year=school_year,
        semester=semester,
        head_teacher_id=head,
        assistant_teacher_id=assistant,
        art_teacher_id=art,
        grade_id=grade_id,
    )
    s.add(c)
    s.flush()
    return c


def test_term_headcounts_past_reads_snapshot(db):
    """過去學期：期初讀開學月快照、期末讀期末月快照、跨 age_group 加總。"""
    s, _ = db
    c = _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=1)
    # 下學期 113-2：開學月=西元(113+1911+1)=2025/2、期末月=2025/7
    s.add_all(
        [
            MonthlyEnrollmentSnapshot(
                year=2025, month=2, classroom_id=c.id, age_group="3-4", total_count=10
            ),
            MonthlyEnrollmentSnapshot(
                year=2025, month=2, classroom_id=c.id, age_group="4-5", total_count=12
            ),
            MonthlyEnrollmentSnapshot(
                year=2025, month=7, classroom_id=c.id, age_group="3-4", total_count=20
            ),
        ]
    )
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 113, 2, is_current=False)
    assert start == 22
    assert end == 20
    assert is_live is False


def test_term_headcounts_no_snapshot_returns_none(db):
    """過去學期無快照 → start/end 皆 None。"""
    s, _ = db
    c = _mk_classroom(s, name="無料班", school_year=112, semester=1, head=1)
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 112, 1, is_current=False)
    assert start is None
    assert end is None
    assert is_live is False


def test_term_headcounts_current_uses_live_end(db):
    """當前學期：期末=即時在籍數、is_live=True。"""
    s, _ = db
    c = _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=1)
    s.add_all(
        [
            Student(
                student_id="S1",
                name="生一",
                classroom_id=c.id,
                enrollment_date=date(2024, 8, 1),
            ),
            Student(
                student_id="S2",
                name="生二",
                classroom_id=c.id,
                enrollment_date=date(2024, 8, 1),
            ),
        ]
    )
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 114, 2, is_current=True)
    assert end == 2
    assert is_live is True
