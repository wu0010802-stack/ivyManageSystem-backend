"""
測試節慶獎金「本期累積」相關純函式與 engine 方法。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary.utils import (
    get_current_period_passed_months,
    get_distribution_period_months,
)


class TestGetCurrentPeriodPassedMonths:
    def test_distribution_months_return_empty(self):
        for m in (2, 6, 9, 12):
            assert get_current_period_passed_months(2026, m) == []

    def test_january_crosses_previous_year(self):
        assert get_current_period_passed_months(2026, 1) == [(2025, 12), (2026, 1)]

    def test_march_has_two_months(self):
        assert get_current_period_passed_months(2026, 3) == [(2026, 2), (2026, 3)]

    def test_april_has_three_months(self):
        assert get_current_period_passed_months(2026, 4) == [
            (2026, 2),
            (2026, 3),
            (2026, 4),
        ]

    def test_may_has_four_months(self):
        assert get_current_period_passed_months(2026, 5) == [
            (2026, 2),
            (2026, 3),
            (2026, 4),
            (2026, 5),
        ]

    def test_july_has_two_months(self):
        assert get_current_period_passed_months(2026, 7) == [(2026, 6), (2026, 7)]

    def test_august_has_three_months(self):
        assert get_current_period_passed_months(2026, 8) == [
            (2026, 6),
            (2026, 7),
            (2026, 8),
        ]

    def test_october_has_two_months(self):
        assert get_current_period_passed_months(2026, 10) == [(2026, 9), (2026, 10)]

    def test_november_has_three_months(self):
        assert get_current_period_passed_months(2026, 11) == [
            (2026, 9),
            (2026, 10),
            (2026, 11),
        ]


class TestGetDistributionPeriodMonths:
    """發放月所結算的月份清單（不含發放月本身）。"""

    def test_non_distribution_month_returns_empty(self):
        for m in (1, 3, 4, 5, 7, 8, 10, 11):
            assert get_distribution_period_months(2026, m) == []

    def test_february_crosses_year_boundary(self):
        assert get_distribution_period_months(2026, 2) == [(2025, 12), (2026, 1)]

    def test_june_covers_feb_to_may(self):
        assert get_distribution_period_months(2026, 6) == [
            (2026, 2),
            (2026, 3),
            (2026, 4),
            (2026, 5),
        ]

    def test_september_covers_jun_to_aug(self):
        assert get_distribution_period_months(2026, 9) == [
            (2026, 6),
            (2026, 7),
            (2026, 8),
        ]

    def test_december_covers_sep_to_nov(self):
        assert get_distribution_period_months(2026, 12) == [
            (2026, 9),
            (2026, 10),
            (2026, 11),
        ]


# ---------- 新增：engine.calculate_period_accrual_row 測試 ----------

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    Employee,
    MeetingRecord,
    Student,
)
from services.salary_engine import SalaryEngine


@pytest.fixture
def db_session(tmp_path):
    """in-memory sqlite + Base.create_all，回傳 session factory。"""
    db_path = tmp_path / "period-accrual-test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def salary_engine_no_db():
    """SalaryEngine 實例（不從 DB 載入 config，使用預設常數）。"""
    return SalaryEngine(load_from_db=False)


def _make_classroom_teacher(
    session,
    *,
    hire_date=date(2024, 1, 1),
    employee_id="E001",
    student_id_prefix="S",
):
    """建立一位有班級的帶班老師 + 20 位在籍學生。"""
    grade = ClassGrade(name="大班")
    session.add(grade)
    session.flush()

    emp = Employee(
        employee_id=employee_id,
        name="王老師",
        title="幼兒園教師",
        position="幼兒園教師",
        base_salary=35000,
        hire_date=hire_date,
        is_active=True,
    )
    session.add(emp)
    session.flush()

    classroom = Classroom(
        name="向日葵班",
        grade_id=grade.id,
        head_teacher_id=emp.id,
        assistant_teacher_id=0,
        is_active=True,
    )
    session.add(classroom)
    session.flush()

    emp.classroom_id = classroom.id

    for i in range(20):
        session.add(
            Student(
                student_id=f"{student_id_prefix}{i:03d}",
                name=f"學生{i}",
                classroom_id=classroom.id,
                enrollment_date=date(2024, 1, 1),
                is_active=True,
            )
        )
    session.commit()
    return emp, classroom


class TestCalculatePeriodAccrualRow:
    def test_classroom_teacher_normal_month(self, db_session, salary_engine_no_db):
        with db_session() as session:
            emp, classroom = _make_classroom_teacher(session)

            ctx = {"session": session, "employee": emp, "classroom": classroom}
            result = salary_engine_no_db.calculate_period_accrual_row(
                emp.id, 2026, 4, _ctx=ctx
            )

        assert "festival_bonus" in result
        assert "overtime_bonus" in result
        assert "meeting_absence_deduction" in result
        assert "category" in result
        assert result["category"] == "帶班老師"
        assert result["festival_bonus"] >= 0
        assert result["overtime_bonus"] >= 0
        assert result["meeting_absence_deduction"] == 0  # 無會議紀錄

    def test_meeting_absence_deduction_counts_absent(
        self, db_session, salary_engine_no_db
    ):
        with db_session() as session:
            emp, classroom = _make_classroom_teacher(session)
            # 4 月開三次會議，缺席兩次
            for d, attended in [
                (date(2026, 4, 5), True),
                (date(2026, 4, 12), False),
                (date(2026, 4, 19), False),
            ]:
                session.add(
                    MeetingRecord(
                        employee_id=emp.id,
                        meeting_date=d,
                        attended=attended,
                        overtime_pay=0,
                    )
                )
            session.commit()

            ctx = {"session": session, "employee": emp, "classroom": classroom}
            result = salary_engine_no_db.calculate_period_accrual_row(
                emp.id, 2026, 4, _ctx=ctx
            )

        penalty = salary_engine_no_db._meeting_absence_penalty
        assert result["meeting_absence_deduction"] == 2 * penalty

    def test_ineligible_new_hire_zero_bonus(self, db_session, salary_engine_no_db):
        with db_session() as session:
            # 2026/4 查詢；hire 2026/3/1 → 月底為 reference_date 仍未滿 3 個月
            emp, classroom = _make_classroom_teacher(
                session, hire_date=date(2026, 3, 1)
            )

            ctx = {"session": session, "employee": emp, "classroom": classroom}
            result = salary_engine_no_db.calculate_period_accrual_row(
                emp.id, 2026, 4, _ctx=ctx
            )

        assert result["festival_bonus"] == 0
        # 未滿 3 個月：overtime 也必須為 0（與 calculate_salary 路徑一致）
        # 2026-04-27 改造：以前 overtime 不套 eligibility，現在統一套；此斷言
        # 防止 future revert 把 overtime 拉回 buggy 路徑。
        assert result["overtime_bonus"] == 0

    def test_non_classroom_employee_has_zero_overtime(
        self, db_session, salary_engine_no_db
    ):
        """辦公室職員沒有 classroom → overtime_bonus = 0。"""
        with db_session() as session:
            emp = Employee(
                employee_id="E_OFFICE_01",
                name="李會計",
                title="職員",
                position="職員",
                base_salary=32000,
                hire_date=date(2024, 1, 1),
                is_active=True,
            )
            session.add(emp)
            session.commit()

            ctx = {"session": session, "employee": emp, "classroom": None}
            result = salary_engine_no_db.calculate_period_accrual_row(
                emp.id, 2026, 4, _ctx=ctx
            )

        assert result["overtime_bonus"] == 0

    def test_engine_returns_raw_deduction_without_capping(
        self, db_session, salary_engine_no_db
    ):
        """engine 層回傳 raw 扣款值（不做 max(0, ...) 夾逼），夾逼於端點層 totals。

        此測試證明單月 row 不對 meeting_absence_deduction 套用上限，因此端點層
        net_estimate = max(0, fb+ot-ded) 的夾逼確實有意義。
        """
        with db_session() as session:
            emp, classroom = _make_classroom_teacher(session)
            for d, attended in [
                (date(2026, 4, 5), False),
                (date(2026, 4, 12), False),
            ]:
                session.add(
                    MeetingRecord(
                        employee_id=emp.id,
                        meeting_date=d,
                        attended=attended,
                        overtime_pay=0,
                    )
                )
            session.commit()

            ctx = {"session": session, "employee": emp, "classroom": classroom}
            result = salary_engine_no_db.calculate_period_accrual_row(
                emp.id, 2026, 4, _ctx=ctx
            )

        penalty = salary_engine_no_db._meeting_absence_penalty
        # 不論 festival/overtime 為何，單月扣款值固定為 absent_count × penalty
        assert result["meeting_absence_deduction"] == 2 * penalty
