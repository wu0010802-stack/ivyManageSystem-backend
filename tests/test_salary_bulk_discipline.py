"""F1 回歸測試：bulk 薪資路徑須與 single 一致，在發放月把 pending 懲處
從節慶+超額獎金扣減並標記 applied。

Bug（2026-06-02 修補前）：process_bulk_salary_calculation /
_compute_and_persist_single_employee 漏呼叫 _adjust_period_totals_for_discipline
與 _mark_discipline_applied，導致月底整批結算的員工不會被扣懲處（多付獎金）、
懲處停在 pending；只有個別重算（process_salary_calculation）才會扣 → 兩路徑不一致。
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
    DisciplinaryAction,
    Employee,
    SalaryRecord,
    Student,
)
from services.salary_engine import SalaryEngine


@pytest.fixture
def salary_engine_db(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'bulk-disc.sqlite'}",
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


def _teacher(session, emp_id="DISC_T", name="懲處老師"):
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


def _students(session, classroom_id, count, prefix):
    for i in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{i:03d}",
                name=f"{prefix}{i}",
                classroom_id=classroom_id,
                enrollment_date=date(2024, 8, 1),  # 早於 Feb period（Dec/Jan）
                is_active=True,
            )
        )


def _bonus_and_record(session, emp_id, year=2026, month=2):
    r = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == emp_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .one()
    )
    bonus = float(r.festival_bonus or 0) + float(r.overtime_bonus or 0)
    return bonus, r.id


def test_bulk_deducts_pending_discipline_from_bonus(salary_engine_db):
    """發放月（Feb）bulk 結算：pending 懲處須從節慶+超額扣減並標記 applied。"""
    engine, session_factory = salary_engine_db
    DEDUCTION = 1000  # warning 預設金額；確保 < 基準獎金以完整扣減

    with session_factory() as s:
        grade = ClassGrade(name="大班", is_active=True)
        s.add(grade)
        s.flush()
        t = _teacher(s)
        cls = Classroom(
            name="甲班", grade_id=grade.id, head_teacher_id=t.id, is_active=True
        )
        s.add(cls)
        s.flush()
        t.classroom_id = cls.id
        _students(s, cls.id, 27, "S")
        s.commit()
        emp_id = t.id

    # ── 基準：無懲處時的節慶+超額合計 ──
    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with session_factory() as s:
        base_bonus, _ = _bonus_and_record(s, emp_id)
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == 2026, SalaryRecord.salary_month == 2
        ).delete()
        s.commit()
    assert (
        base_bonus > DEDUCTION
    ), f"前置條件失敗：基準獎金 {base_bonus} 須 > {DEDUCTION} 才能完整扣減"

    # ── 加 pending 懲處後再 bulk ──
    with session_factory() as s:
        s.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=DEDUCTION,
            )
        )
        s.commit()

    engine.process_bulk_salary_calculation([emp_id], 2026, 2)

    with session_factory() as s:
        new_bonus, record_id = _bonus_and_record(s, emp_id)
        action = s.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        applied_to = action.applied_to_salary_id

    # 1) 獎金被扣減 DEDUCTION（金額正確性 — bug 的核心）
    assert base_bonus - new_bonus == DEDUCTION, (
        f"bulk 未從獎金扣減懲處：base={base_bonus} new={new_bonus}"
        f"（差額應為 {DEDUCTION}）"
    )
    # 2) 懲處被標記 applied 到本月薪資記錄（避免重複扣 / 與 single 一致）
    assert applied_to == record_id, "bulk 未把 pending 懲處標記為已抵扣"
