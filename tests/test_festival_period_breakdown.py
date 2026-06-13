"""發放月節慶/超額獎金「逐月明細」測試。

需求（2026-06-13）：發放月（2/6/9/12）點開 festival_bonus 欄位拆帳時，
應回傳該期涵蓋各月（如 6 月 → 2~5 月）逐月一列的明細，且逐月加總
須等於入帳金額；非發放月維持既有單列行為。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    Employee,
    LeaveRecord,
    SalaryRecord,
    Student,
)
from services.finance.salary_field_breakdown import (
    build_field_breakdown,
    build_salary_debug_snapshot,
)
from services.salary_engine import SalaryEngine


@pytest.fixture
def breakdown_db(tmp_path):
    """隔離 sqlite DB + 預設常數 engine（空設定表 → 引擎內建預設）。"""
    db_path = tmp_path / "festival-period-breakdown.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    yield SalaryEngine(load_from_db=False), session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_head_teacher_with_class(session, *, enrollment: int = 24):
    """大班單師帶班老師 + 在籍學生（涵蓋期前已入學）。"""
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()

    teacher = Employee(
        employee_id="T700",
        name="逐月明細老師",
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    session.add(teacher)
    session.flush()

    classroom = Classroom(
        name="逐月班",
        grade_id=grade.id,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    session.add(classroom)
    session.flush()
    teacher.classroom_id = classroom.id

    for idx in range(enrollment):
        session.add(
            Student(
                student_id=f"P700{idx:03d}",
                name=f"逐月學生{idx}",
                classroom_id=classroom.id,
                enrollment_date=date(2025, 9, 1),
                is_active=True,
            )
        )
    session.flush()
    return teacher


def _make_record(session, emp, year, month, *, festival_bonus):
    record = SalaryRecord(
        employee_id=emp.id,
        salary_year=year,
        salary_month=month,
        festival_bonus=festival_bonus,
        is_finalized=False,
        version=1,
    )
    session.add(record)
    session.flush()
    return record


class TestFestivalPeriodBreakdown:
    def test_payout_month_returns_one_row_per_covered_month(self, breakdown_db):
        """6 月發放：rows 應為 2~5 月各一列，逐月加總＝入帳金額。"""
        engine, session_factory = breakdown_db
        with session_factory() as session:
            emp = _seed_head_teacher_with_class(session, enrollment=24)
            # 預設常數：基數 2000、大班單師目標 12 → 每月 round(2000*24/12)=4000
            record = _make_record(session, emp, 2026, 6, festival_bonus=4000 * 4)

            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 6)
            data = build_field_breakdown(record, emp, snapshot, "festival_bonus")

            months = [(row["year"], row["month"]) for row in data["rows"]]
            assert months == [(2026, 2), (2026, 3), (2026, 4), (2026, 5)]
            assert sum(row["result"] for row in data["rows"]) == record.festival_bonus
            assert data["summary"]["amount"] == record.festival_bonus
            # 逐月列須帶計算要素（基數/目標/在籍/達成率）
            first = data["rows"][0]
            assert first["bonusBase"] == 2000
            assert first["targetEnrollment"] == 12
            assert first["currentEnrollment"] == 24
            assert "%" in first["ratio"]

    def test_non_payout_month_keeps_legacy_single_row(self, breakdown_db):
        """5 月非發放月：維持既有單列（當月）行為。"""
        engine, session_factory = breakdown_db
        with session_factory() as session:
            emp = _seed_head_teacher_with_class(session)
            record = _make_record(session, emp, 2026, 5, festival_bonus=0)

            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 5)
            data = build_field_breakdown(record, emp, snapshot, "festival_bonus")

            assert len(data["rows"]) == 1
            assert data["rows"][0]["name"] == emp.name

    def test_skip_month_shows_zero_with_reason(self, breakdown_db):
        """涵蓋月有產假 → 該月列金額 0 並標註不計入。"""
        engine, session_factory = breakdown_db
        with session_factory() as session:
            emp = _seed_head_teacher_with_class(session, enrollment=12)
            session.add(
                LeaveRecord(
                    employee_id=emp.id,
                    leave_type="maternity",
                    start_date=date(2026, 3, 5),
                    end_date=date(2026, 3, 20),
                    leave_hours=80,
                    status="approved",
                )
            )
            session.flush()
            # 2/4/5 月各 2000（達成率 100%），3 月產假不計入
            record = _make_record(session, emp, 2026, 6, festival_bonus=6000)

            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 6)
            data = build_field_breakdown(record, emp, snapshot, "festival_bonus")

            march = next(
                row for row in data["rows"] if (row["year"], row["month"]) == (2026, 3)
            )
            assert march["result"] == 0
            assert "不計入" in march["remark"]
            assert sum(row["result"] for row in data["rows"]) == 6000

    def test_adjustment_row_added_when_persisted_amount_differs(self, breakdown_db):
        """入帳金額（含懲處抵扣/手調）與逐月合計不同 → 補一列調整使表格合計對帳。"""
        engine, session_factory = breakdown_db
        with session_factory() as session:
            emp = _seed_head_teacher_with_class(session, enrollment=12)
            # 逐月合計 8000，入帳 7500（模擬懲處抵扣 500）
            record = _make_record(session, emp, 2026, 6, festival_bonus=7500)

            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 6)
            data = build_field_breakdown(record, emp, snapshot, "festival_bonus")

            assert sum(row["result"] for row in data["rows"]) == 7500
            adjustment = data["rows"][-1]
            assert adjustment["result"] == -500
            assert "調整" in adjustment["remark"] or "抵扣" in adjustment["remark"]

    def test_overtime_bonus_payout_month_monthly_rows(self, breakdown_db):
        """超額獎金欄在發放月同樣逐月呈現。"""
        engine, session_factory = breakdown_db
        with session_factory() as session:
            emp = _seed_head_teacher_with_class(session, enrollment=24)
            record = _make_record(session, emp, 2026, 6, festival_bonus=4000 * 4)
            # 預設常數：大班單師超額目標 13 → 超額 11 人 × 400 = 4400/月
            record.overtime_bonus = 4400 * 4
            session.flush()

            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 6)
            data = build_field_breakdown(record, emp, snapshot, "overtime_bonus")

            months = [(row["year"], row["month"]) for row in data["rows"]]
            assert months == [(2026, 2), (2026, 3), (2026, 4), (2026, 5)]
            assert sum(row["result"] for row in data["rows"]) == int(
                record.overtime_bonus
            )
