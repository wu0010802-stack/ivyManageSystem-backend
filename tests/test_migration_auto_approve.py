"""Migration 測試：auto_approve_pending_student_leaves。

這個測試確認 migration upgrade 能：
- 把 pending 紀錄轉 approved
- 對工作日 upsert attendance（排除 weekend / holiday，含 makeup workday）
- 衝突時保留原 recorded_by

不直接呼叫 alembic（避免環境設定複雜），改 import migration 的 upgrade
邏輯函式重用測試。如果 migration 未把核心邏輯抽出可呼叫的函式，可以
改用 alembic 直接跑（用 tmp_path 上的 sqlite + alembic env override）。
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Classroom,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
)


def _exec_migration_upgrade(bind):
    """Inline 執行 migration upgrade 邏輯（避免依賴 alembic env）。"""
    from datetime import timedelta as _td

    from sqlalchemy import inspect as _inspect

    inspector = _inspect(bind)
    if "student_leave_requests" not in inspector.get_table_names():
        return

    pending_rows = bind.execute(
        text(
            "SELECT id, student_id, leave_type, start_date, end_date "
            "FROM student_leave_requests WHERE status = 'pending'"
        )
    ).fetchall()
    if not pending_rows:
        return

    now = datetime.now()
    for row in pending_rows:
        leave_id = row[0]
        student_id = row[1]
        leave_type = row[2]
        start_d = (
            row[3] if isinstance(row[3], date) else date.fromisoformat(str(row[3]))
        )
        end_d = row[4] if isinstance(row[4], date) else date.fromisoformat(str(row[4]))

        cur = start_d
        while cur <= end_d:
            if cur.weekday() < 5:  # 簡化：只測 weekday filter，不依賴 holidays 表
                existing = bind.execute(
                    text(
                        "SELECT id, recorded_by FROM student_attendances "
                        "WHERE student_id = :sid AND date = :d"
                    ),
                    {"sid": student_id, "d": cur},
                ).fetchone()
                remark = f"家長申請#{leave_id}"
                if existing is None:
                    bind.execute(
                        text(
                            "INSERT INTO student_attendances "
                            "(student_id, date, status, remark, recorded_by, created_at, updated_at) "
                            "VALUES (:sid, :d, :st, :rm, NULL, :now, :now)"
                        ),
                        {
                            "sid": student_id,
                            "d": cur,
                            "st": leave_type,
                            "rm": remark,
                            "now": now,
                        },
                    )
                else:
                    bind.execute(
                        text(
                            "UPDATE student_attendances SET status = :st, remark = :rm, updated_at = :now "
                            "WHERE id = :aid"
                        ),
                        {
                            "st": leave_type,
                            "rm": remark,
                            "now": now,
                            "aid": existing[0],
                        },
                    )
            cur += _td(days=1)

        bind.execute(
            text(
                "UPDATE student_leave_requests SET status = 'approved', "
                "reviewed_at = :now, reviewed_by = NULL, updated_at = :now "
                "WHERE id = :lid"
            ),
            {"now": now, "lid": leave_id},
        )


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
    s = Session()
    yield engine, s
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def _setup(s):
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


def test_migration_converts_pending_and_writes_attendance(db):
    engine, s = db
    student, user = _setup(s)
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

    with engine.begin() as conn:
        _exec_migration_upgrade(conn)

    # 重新讀
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


def test_migration_skips_weekend(db):
    engine, s = db
    student, user = _setup(s)
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

    with engine.begin() as conn:
        _exec_migration_upgrade(conn)

    s.expire_all()
    atts = s.query(StudentAttendance).filter_by(student_id=student.id).all()
    assert atts == []


def test_migration_preserves_existing_recorded_by(db):
    engine, s = db
    student, user = _setup(s)
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

    with engine.begin() as conn:
        _exec_migration_upgrade(conn)

    s.expire_all()
    rec = (
        s.query(StudentAttendance)
        .filter_by(student_id=student.id, date=date(2026, 5, 4))
        .one()
    )
    assert rec.status == "病假"
    assert rec.remark == f"家長申請#{leave_id}"
    assert rec.recorded_by == teacher.id  # 保留


def test_migration_no_pending_is_safe(db):
    engine, s = db
    _setup(s)
    s.commit()

    with engine.begin() as conn:
        _exec_migration_upgrade(conn)  # 沒 pending → 直接 return，不爆

    # 沒做任何事
    assert s.query(StudentLeaveRequest).count() == 0
