"""按日加權在籍人數（L3，spec 2026-06-13-enrollment-count-correctness）。

daily_weighted：在籍人數 = Σ(每日在籍生數) ÷ 當月日曆天數，1 位小數。
邊界與 month_end 模式的 filter 對齊：withdrawal 當日不在籍、graduation 當日在籍。
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.config import BonusConfig
from models.database import Base, ClassGrade, Classroom, Student
from models.student_transfer import StudentClassroomTransfer


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "daily-weighted.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _make_classroom(session, name):
    grade = ClassGrade(name=f"{name}grade", is_active=True)
    session.add(grade)
    session.flush()
    room = Classroom(name=name, grade_id=grade.id, is_active=True)
    session.add(room)
    session.flush()
    return room


def _make_student(session, sid, classroom, **kw):
    student = Student(
        student_id=sid,
        name=f"學生{sid}",
        classroom_id=classroom.id if classroom else None,
        enrollment_date=kw.pop("enrollment_date", date(2025, 9, 1)),
        is_active=kw.pop("is_active", True),
        **kw,
    )
    session.add(student)
    session.flush()
    return student


class TestDailyWeightedCounts:
    def test_full_month_student_counts_one(self, db):
        from services.salary.enrollment_snapshot import compute_live_counts

        with db() as session:
            room = _make_classroom(session, "A")
            _make_student(session, "D001", room)
            counts = compute_live_counts(session, 2026, 3, mode="daily_weighted")
            assert counts["classes"] == {room.id: 1.0}
            assert counts["school"] == 1.0

    def test_mid_month_enrollment_prorated(self, db):
        """3/17 入學（3 月 31 天，在籍 17~31 共 15 天）→ 15/31 ≈ 0.5。"""
        from services.salary.enrollment_snapshot import compute_live_counts

        with db() as session:
            room = _make_classroom(session, "B")
            _make_student(session, "D002", room, enrollment_date=date(2026, 3, 17))
            counts = compute_live_counts(session, 2026, 3, mode="daily_weighted")
            assert counts["classes"] == {room.id: 0.5}

    def test_mid_month_withdrawal_prorated(self, db):
        """3/11 退學（在籍 1~10 共 10 天）→ 10/31 ≈ 0.3；月底快照模式則為 0。"""
        from services.salary.enrollment_snapshot import compute_live_counts

        with db() as session:
            room = _make_classroom(session, "C")
            _make_student(session, "D003", room, withdrawal_date=date(2026, 3, 11))
            weighted = compute_live_counts(session, 2026, 3, mode="daily_weighted")
            assert weighted["classes"] == {room.id: 0.3}
            month_end = compute_live_counts(session, 2026, 3, mode="month_end")
            assert month_end["classes"] == {}

    def test_mid_month_transfer_splits_between_classes(self, db):
        """3/11 從 A 轉 B：A 得 10/31 ≈ 0.3、B 得 21/31 ≈ 0.7。"""
        from services.salary.enrollment_snapshot import compute_live_counts

        with db() as session:
            room_a = _make_classroom(session, "甲")
            room_b = _make_classroom(session, "乙")
            student = _make_student(session, "D004", room_b)
            session.add(
                StudentClassroomTransfer(
                    student_id=student.id,
                    from_classroom_id=room_a.id,
                    to_classroom_id=room_b.id,
                    transferred_at=datetime(2026, 3, 11, 9, 0),
                )
            )
            session.flush()
            counts = compute_live_counts(session, 2026, 3, mode="daily_weighted")
            assert counts["classes"] == {room_a.id: 0.3, room_b.id: 0.7}
            assert counts["school"] == 1.0  # 全校不因轉班重複計

    def test_resolver_uses_bonus_config_mode(self, db):
        """BonusConfig.enrollment_count_mode=daily_weighted → 無快照 fallback 用加權。"""
        from services.salary.enrollment_snapshot import resolve_bonus_counts

        with db() as session:
            room = _make_classroom(session, "丙")
            _make_student(session, "D005", room, withdrawal_date=date(2026, 3, 11))
            session.add(
                BonusConfig(
                    config_year=2026,
                    is_active=True,
                    version=1,
                    enrollment_count_mode="daily_weighted",
                )
            )
            session.flush()

            school, classes = resolve_bonus_counts(session, 2026, 3)
            assert classes == {room.id: 0.3}
            assert school == 0.3
