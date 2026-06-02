"""tests/test_year_end_enrollment_rates.py — 年終達成率純查詢測試（TDD）

覆蓋：
  1. school_achievement_rate — 全校達成率（在籍嚴格 filter：排退學 + lifecycle 非 active）
  2. class_performance_rate  — 班級經營績效（各月底在班平均 / 編制 × 100）
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.student_transfer import StudentClassroomTransfer

# ============ Fixtures ============


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
    c = Classroom(name="測試班", school_year=114, semester=1)
    session.add(c)
    session.flush()
    return c


def _make_student(
    session,
    *,
    name: str = "測試生",
    classroom_id: int | None = None,
    enrollment_date: date | None = None,
    graduation_date: date | None = None,
    withdrawal_date: date | None = None,
    lifecycle_status: str = LIFECYCLE_ACTIVE,
) -> Student:
    s = Student(
        student_id=f"T{session.query(Student).count() + 1:04d}",
        name=name,
        classroom_id=classroom_id,
        lifecycle_status=lifecycle_status,
        enrollment_date=enrollment_date,
        graduation_date=graduation_date,
        withdrawal_date=withdrawal_date,
    )
    session.add(s)
    session.flush()
    return s


# ============ Test: school_achievement_rate ============


class TestSchoolAchievementRate:
    def test_school_achievement_rate_excludes_withdrawn(self, session, classroom):
        """目標 160；在籍 2 人，另 1 人已退學 → actual=2 → rate = 2/160*100 = 1.25"""
        from decimal import Decimal

        basis = date(2026, 3, 31)

        # 在籍學生（enrollment_date <= basis，graduation_date >= basis，withdrawal = None）
        _make_student(
            session,
            name="在籍生A",
            classroom_id=classroom.id,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        _make_student(
            session,
            name="在籍生B",
            classroom_id=classroom.id,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
        )

        # 退學生（withdrawal_date 在 basis 前，lifecycle = withdrawn）
        _make_student(
            session,
            name="退學生C",
            classroom_id=classroom.id,
            enrollment_date=date(2025, 9, 1),
            withdrawal_date=date(2026, 1, 15),
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )

        session.commit()

        from services.year_end.enrollment_rates import school_achievement_rate

        result = school_achievement_rate(session, basis_date=basis, target=160)
        assert result == Decimal("1.25"), f"expected Decimal('1.25'), got {result!r}"

    def test_school_achievement_rate_target_zero_returns_zero(self, session):
        """target <= 0 時不除零，直接回 0.00"""
        from decimal import Decimal

        from services.year_end.enrollment_rates import school_achievement_rate

        result = school_achievement_rate(
            session, basis_date=date(2026, 3, 31), target=0
        )
        assert result == Decimal("0.00")


# ============ Test: class_performance_rate ============


class TestClassPerformanceRate:
    def test_class_performance_rate_avg_over_months(self, session, classroom):
        """某班編制 12；各月底在班 14,14,14,14,13,14 → avg=13.833.../12*100 → 115.28

        設計：14 名學生全部 lifecycle=active（無退學）；其中第 14 名學生透過
        StudentClassroomTransfer 實現班級異動：
          - 2026-02-10 轉到 classroom_b（外班）
          - 2026-03-05 轉回 classroom（原班）
        classroom_at_month_end resolver 依最後一筆 transfer 決定月底歸屬：
          Oct,Nov,Dec,Jan: 無 transfer 記錄 → fallback student.classroom_id = classroom → 14 人
          Feb 28: 最後 transfer → classroom_b → 不計入 → 13 人
          Mar 31: 最後 transfer → classroom → 計入 → 14 人
        → (14+14+14+14+13+14)/6 / 12 * 100 = 115.277... → ROUND_HALF_UP 2dp → 115.28
        """
        from decimal import Decimal

        # 建第二個班級（轉入班）
        classroom_b = Classroom(name="外班", school_year=114, semester=1)
        session.add(classroom_b)
        session.flush()

        # 月底日期（6 個月底）
        month_ends = [
            date(2025, 10, 31),
            date(2025, 11, 30),
            date(2025, 12, 31),
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
        ]

        # 建 14 個學生，全部 lifecycle=active，classroom_id = classroom.id
        for i in range(14):
            _make_student(
                session,
                name=f"班生{i+1:02d}",
                classroom_id=classroom.id,
                enrollment_date=date(2025, 9, 1),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )

        # 第 14 名學生在 2 月暫時轉到外班，3 月初轉回
        student_x = session.query(Student).order_by(Student.id.desc()).first()
        # transfer 1: 2026-02-10 轉出到 classroom_b
        t1 = StudentClassroomTransfer(
            student_id=student_x.id,
            from_classroom_id=classroom.id,
            to_classroom_id=classroom_b.id,
            transferred_at=datetime(2026, 2, 10, 9, 0, 0),
        )
        # transfer 2: 2026-03-05 轉回 classroom
        t2 = StudentClassroomTransfer(
            student_id=student_x.id,
            from_classroom_id=classroom_b.id,
            to_classroom_id=classroom.id,
            transferred_at=datetime(2026, 3, 5, 9, 0, 0),
        )
        session.add_all([t1, t2])
        session.commit()

        from services.year_end.enrollment_rates import class_performance_rate

        result = class_performance_rate(
            session,
            classroom_id=classroom.id,
            month_ends=month_ends,
            head_count_target=12,
        )

        # 期望：(14+14+14+14+13+14)/6 / 12 * 100
        # = 83/6 / 12 * 100 = 13.8333.../12*100 = 115.277... → ROUND_HALF_UP 2dp → 115.28
        assert result == Decimal(
            "115.28"
        ), f"expected Decimal('115.28'), got {result!r}"

    def test_class_performance_rate_target_zero_returns_zero(self, session, classroom):
        """head_count_target <= 0 → 不除零，回 0.00"""
        from decimal import Decimal

        from services.year_end.enrollment_rates import class_performance_rate

        result = class_performance_rate(
            session,
            classroom_id=classroom.id,
            month_ends=[date(2026, 3, 31)],
            head_count_target=0,
        )
        assert result == Decimal("0.00")

    def test_class_performance_rate_empty_month_ends_returns_zero(
        self, session, classroom
    ):
        """month_ends 為空 → 回 0.00（無法計算）"""
        from decimal import Decimal

        from services.year_end.enrollment_rates import class_performance_rate

        result = class_performance_rate(
            session,
            classroom_id=classroom.id,
            month_ends=[],
            head_count_target=12,
        )
        assert result == Decimal("0.00")
