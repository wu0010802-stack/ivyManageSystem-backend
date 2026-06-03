"""F2a 前置 characterization：single↔bulk「計算尾段」parity。

守住 _finalize_breakdown 抽取前後行為不變。聚焦兩路徑尾段「唯一的差異點」：
- supplementary：single 即時 query ytd / bulk 用 preload.ytd_bonus_by_emp →
  必須同值（這是 F2a 抽取的關鍵守衛——抽取後兩路徑共用同一段，ytd_before
  仍由各 caller 傳入）。
- discipline：兩路徑都從節慶+超額扣 pending 懲處 → 必須同值。

應在 F2a 重構「之前」即綠（characterization test）；若紅 = 發現既有 single/bulk
分歧（bug），非 F2a 回歸。

（hourly / proration / office·supervisor / waived 等情境守的是 F2b 的「輸入建構」
收斂，留待 F2b go/no-go 時再補，不在此前載。）
"""

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    DisciplinaryAction,
    Employee,
    SalaryRecord,
    Student,
)
from services.salary_engine import SalaryEngine

PERIOD_SCHOOL_YEAR = 114
PERIOD_SEMESTER = 1

# 比對「錢相關 + 補充保費 + 獎金」全欄位，任何尾段分歧都會被抓到
_PARITY_FIELDS = (
    "festival_bonus",
    "overtime_bonus",
    "performance_bonus",
    "special_bonus",
    "supervisor_dividend",
    "gross_salary",
    "labor_insurance_employee",
    "health_insurance_employee",
    "pension_employee",
    "supplementary_health_employee",
    "leave_deduction",
    "absence_deduction",
    "total_deduction",
    "net_salary",
)


@pytest.fixture
def salary_engine_db(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'finalize-parity.sqlite'}",
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


def _teacher(session, emp_id="FP_T", name="尾段老師"):
    t = Employee(
        employee_id=emp_id,
        name=name,
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(t)
    session.flush()
    return t


def _term_classroom(session, name, grade_id, head_id):
    c = Classroom(
        name=name,
        grade_id=grade_id,
        head_teacher_id=head_id,
        school_year=PERIOD_SCHOOL_YEAR,
        semester=PERIOD_SEMESTER,
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


def _students(session, classroom_id, count, prefix):
    for i in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{i:03d}",
                name=f"{prefix}{i}",
                classroom_id=classroom_id,
                enrollment_date=date(2024, 8, 1),
                is_active=True,
            )
        )


def _setup_teacher_with_bonus(session):
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()
    t = _teacher(session)
    c = _term_classroom(session, "甲班", grade.id, t.id)
    t.classroom_id = c.id
    _students(session, c.id, 27, "S")
    session.flush()
    return t.id


def _snap(session_factory, emp_id, year=2026, month=2):
    with session_factory() as s:
        r = (
            s.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == emp_id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .one()
        )
        return {f: float(getattr(r, f) or 0) for f in _PARITY_FIELDS}


def _delete_feb(session_factory):
    with session_factory() as s:
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == 2026, SalaryRecord.salary_month == 2
        ).delete()
        s.commit()


def test_supplementary_parity_single_vs_bulk(salary_engine_db):
    """補充保費：single 即時查 ytd vs bulk 用預載 ytd → 結果必須完全相同，且須真的 fire。"""
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        emp_id = _setup_teacher_with_bonus(s)
        # 種一筆 1 月 record，使 ytd_before（200000）> 4×投保(30000×4=120000)門檻，
        # 確保 2 月當月獎金全額落入補充保費計算（excess = 當月獎金）。
        s.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=1,
                performance_bonus=Decimal("200000"),
            )
        )
        s.commit()

    # 單筆路徑（內部 query ytd）
    engine.process_salary_calculation(emp_id, 2026, 2)
    single = _snap(session_factory, emp_id)
    _delete_feb(session_factory)  # 只刪 2 月，1 月 ytd 來源保留

    # 批次路徑（用 preload.ytd_bonus_by_emp）
    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    bulk = _snap(session_factory, emp_id)

    assert single["supplementary_health_employee"] > 0, "補充保費未 fire，測試無效"
    assert single == bulk, f"尾段 parity 失敗\nsingle={single}\nbulk={bulk}"


def test_discipline_parity_single_vs_bulk(salary_engine_db):
    """懲處扣減：single 與 bulk 從獎金扣 pending 懲處的結果必須相同。"""
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        emp_id = _setup_teacher_with_bonus(s)
        s.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=1000,
            )
        )
        s.commit()

    # 單筆路徑
    engine.process_salary_calculation(emp_id, 2026, 2)
    single = _snap(session_factory, emp_id)
    with session_factory() as s:
        single_applied = (
            s.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        ).applied_to_salary_id

    # 重置：刪 2 月 record + 還原懲處為未抵扣（applied_* 全清，否則 bulk 查不到 pending）
    _delete_feb(session_factory)
    with session_factory() as s:
        a = s.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        a.applied_to_salary_id = None
        a.applied_at = None
        a.applied_amount = None
        s.commit()

    # 批次路徑
    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    bulk = _snap(session_factory, emp_id)
    with session_factory() as s:
        bulk_applied = (
            s.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        ).applied_to_salary_id

    assert (
        single_applied is not None and bulk_applied is not None
    ), "兩路徑都應標記 applied"
    assert single == bulk, f"尾段 parity 失敗\nsingle={single}\nbulk={bulk}"
