"""Migration 測試：auto_approve_pending_student_leaves。

直接 import migration module 並呼叫 upgrade() against 一個 test connection。
這樣可以實測 migration code 本身（含 holidays / workday_overrides 邏輯），
而非平行重做一份簡化版。

Reference: tests/test_recruitment_ivykids_migration.py
"""

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Classroom,
    Holiday,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
    WorkdayOverride,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260502_9e4549832715_auto_approve_pending_student_leaves.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("auto_approve_mig", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _AlembicOpStub:
    """讓 migration 內 `op.get_bind()` 在測試環境下回傳 test connection。"""

    def __init__(self, bind):
        self._bind = bind

    def get_bind(self):
        return self._bind


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "mig.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = Session
    Base.metadata.create_all(engine)
    migration = _load_migration()
    s = Session()
    yield engine, s, migration
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def _run_upgrade(engine, migration, monkeypatch):
    """以 test connection 呼叫 migration.upgrade()。"""
    with engine.begin() as conn:
        stub = _AlembicOpStub(conn)
        # migration 內呼叫 op.get_bind()，把 op 換成 stub。
        # `inspect(bind)` 會自然走 SQLAlchemy 對 connection 的支援。
        monkeypatch.setattr(migration, "op", stub)
        migration.upgrade()


def _setup_family(s):
    classroom = Classroom(name="A", is_active=True)
    s.add(classroom)
    s.flush()
    student = Student(
        student_id="S1", name="小明", classroom_id=classroom.id, is_active=True
    )
    s.add(student)
    user = User(
        username="p", password_hash="!", role="parent", permissions=0, is_active=True
    )
    s.add(user)
    s.flush()
    return student, user


def test_migration_converts_pending_and_writes_attendance(db, monkeypatch):
    engine, s, migration = db
    student, user = _setup_family(s)
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="病假",
        start_date=date(2026, 5, 4),  # 週一
        end_date=date(2026, 5, 5),  # 週二
        status="pending",
    )
    s.add(leave)
    s.commit()
    leave_id = leave.id

    _run_upgrade(engine, migration, monkeypatch)

    s.expire_all()
    rec = s.query(StudentLeaveRequest).filter_by(id=leave_id).one()
    assert rec.status == "approved"
    assert rec.reviewed_by is None
    assert rec.reviewed_at is not None

    atts = (
        s.query(StudentAttendance)
        .filter_by(student_id=student.id)
        .order_by(StudentAttendance.date)
        .all()
    )
    assert len(atts) == 2
    for a in atts:
        assert a.status == "病假"
        assert a.recorded_by is None
        assert a.remark == f"家長申請#{leave_id}"


def test_migration_skips_weekend(db, monkeypatch):
    engine, s, migration = db
    student, user = _setup_family(s)
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="事假",
        start_date=date(2026, 5, 9),  # 週六
        end_date=date(2026, 5, 10),  # 週日
        status="pending",
    )
    s.add(leave)
    s.commit()

    _run_upgrade(engine, migration, monkeypatch)

    s.expire_all()
    atts = s.query(StudentAttendance).filter_by(student_id=student.id).all()
    assert atts == []


def test_migration_skips_holiday(db, monkeypatch):
    """holiday filter 實測 — migration 應跳過 active holiday。"""
    engine, s, migration = db
    student, user = _setup_family(s)
    s.add(Holiday(date=date(2026, 5, 4), name="補放假", is_active=True))
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="病假",
        start_date=date(2026, 5, 4),  # 週一但被設為 holiday
        end_date=date(2026, 5, 5),  # 週二一般工作日
        status="pending",
    )
    s.add(leave)
    s.commit()

    _run_upgrade(engine, migration, monkeypatch)

    s.expire_all()
    atts = (
        s.query(StudentAttendance)
        .filter_by(student_id=student.id)
        .order_by(StudentAttendance.date)
        .all()
    )
    # 只應有 5/5（5/4 是 holiday）
    assert len(atts) == 1
    assert atts[0].date == date(2026, 5, 5)


def test_migration_includes_makeup_workday(db, monkeypatch):
    """makeup workday filter 實測 — migration 應把 makeup 當應到日。"""
    engine, s, migration = db
    student, user = _setup_family(s)
    s.add(WorkdayOverride(date=date(2026, 5, 9), name="補上班日", is_active=True))
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="事假",
        start_date=date(2026, 5, 9),  # 週六，但被設為 makeup workday
        end_date=date(2026, 5, 9),
        status="pending",
    )
    s.add(leave)
    s.commit()

    _run_upgrade(engine, migration, monkeypatch)

    s.expire_all()
    atts = s.query(StudentAttendance).filter_by(student_id=student.id).all()
    assert len(atts) == 1
    assert atts[0].date == date(2026, 5, 9)


def test_migration_preserves_existing_recorded_by(db, monkeypatch):
    engine, s, migration = db
    student, user = _setup_family(s)
    teacher = User(
        username="t",
        password_hash="!",
        role="teacher",
        permissions=0,
        is_active=True,
    )
    s.add(teacher)
    s.flush()
    s.add(
        StudentAttendance(
            student_id=student.id,
            date=date(2026, 5, 4),
            status="到校",
            remark="教師手寫",
            recorded_by=teacher.id,
        )
    )
    leave = StudentLeaveRequest(
        student_id=student.id,
        applicant_user_id=user.id,
        leave_type="病假",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 4),
        status="pending",
    )
    s.add(leave)
    s.commit()
    leave_id = leave.id

    _run_upgrade(engine, migration, monkeypatch)

    s.expire_all()
    rec = (
        s.query(StudentAttendance)
        .filter_by(student_id=student.id, date=date(2026, 5, 4))
        .one()
    )
    assert rec.status == "病假"
    assert rec.remark == f"家長申請#{leave_id}"
    assert rec.recorded_by == teacher.id  # 保留


def test_migration_no_pending_is_safe(db, monkeypatch):
    engine, s, migration = db
    _setup_family(s)
    s.commit()

    _run_upgrade(engine, migration, monkeypatch)  # 沒 pending → early return

    assert s.query(StudentLeaveRequest).count() == 0
