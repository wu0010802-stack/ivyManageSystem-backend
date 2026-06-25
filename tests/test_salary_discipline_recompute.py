"""懲處群集回歸測試（2026-06-25 稽核）：

P1-A 重算冪等：發放月薪資「重算」時，已抵扣（applied）的懲處不可被還原。
    Bug：_adjust_period_totals_for_discipline / apply_deductions 只取
    applied_to_salary_id IS NULL 的 pending 動作；首次計算後懲處已標 applied
    綁定本月 record，重算時取不到 → festival/overtime 以 raw 覆寫 reduced 值
    → 懲處被靜默還原、員工多領。

P2-D ledger 金額：_mark_discipline_applied 用「扣減後」的 record festival+overtime
    當 available_bonus，當 target > 已扣減後可用額時，applied_amount 被截斷成
    小於實際扣薪金額 → 稽核帳少記。

C#1 admin 設定：engine 從不賦值 self._bonus_config（只賦 _bonus_config_id），
    導致 deduction_amount=0 的懲處一律走 hardcode 1000/3000，忽略 admin 在
    BonusConfig 設定的 warning_deduction/minor_offense_deduction。
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
    BonusConfig,
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
        f"sqlite:///{tmp_path / 'disc-recompute.sqlite'}",
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


def _setup_teacher(session_factory):
    """建班導 + 班級 + 27 學生，確保發放月（Feb）有可觀的節慶/超額獎金。"""
    with session_factory() as s:
        grade = ClassGrade(name="大班", is_active=True)
        s.add(grade)
        s.flush()
        t = Employee(
            employee_id="DISC_RC",
            name="懲處重算老師",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            insurance_salary_level=30000,
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        s.add(t)
        s.flush()
        cls = Classroom(
            name="甲班", grade_id=grade.id, head_teacher_id=t.id, is_active=True
        )
        s.add(cls)
        s.flush()
        t.classroom_id = cls.id
        for i in range(27):
            s.add(
                Student(
                    student_id=f"RC{i:03d}",
                    name=f"RC{i}",
                    classroom_id=cls.id,
                    enrollment_date=date(2024, 8, 1),
                    is_active=True,
                )
            )
        s.commit()
        return t.id


def _bonus_and_record_id(session, emp_id, year=2026, month=2):
    r = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == emp_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .one()
    )
    return float(r.festival_bonus or 0) + float(r.overtime_bonus or 0), r.id


def _delete_record(session_factory, year=2026, month=2):
    with session_factory() as s:
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == year, SalaryRecord.salary_month == month
        ).delete()
        s.commit()


# ─────────────────────────────────────────────────────────────────────────────
# P1-A：重算冪等（single 路徑）
# ─────────────────────────────────────────────────────────────────────────────
def test_single_recompute_keeps_discipline_deduction(salary_engine_db):
    engine, sf = salary_engine_db
    emp_id = _setup_teacher(sf)

    # baseline（無懲處）
    engine.process_salary_calculation(emp_id, 2026, 2)
    with sf() as s:
        base_bonus, _ = _bonus_and_record_id(s, emp_id)
    _delete_record(sf)
    assert base_bonus > 1000, f"前置：基準獎金 {base_bonus} 須 > 1000"

    # 加 pending 懲處 1000
    with sf() as s:
        s.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=1000,
            )
        )
        s.commit()

    # 第一次計算：扣減 1000
    engine.process_salary_calculation(emp_id, 2026, 2)
    with sf() as s:
        bonus1, _ = _bonus_and_record_id(s, emp_id)
    assert base_bonus - bonus1 == 1000, f"首算未扣懲處：base={base_bonus} b1={bonus1}"

    # 第二次計算（重算，不刪 record）：懲處須維持已扣，不可被還原
    engine.process_salary_calculation(emp_id, 2026, 2)
    with sf() as s:
        bonus2, _ = _bonus_and_record_id(s, emp_id)
    assert bonus2 == bonus1, (
        f"重算還原了懲處扣款：首算 reduced={bonus1} 重算={bonus2}"
        f"（raw base={base_bonus}）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1-A：重算冪等（bulk 路徑）
# ─────────────────────────────────────────────────────────────────────────────
def test_bulk_recompute_keeps_discipline_deduction(salary_engine_db):
    engine, sf = salary_engine_db
    emp_id = _setup_teacher(sf)

    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with sf() as s:
        base_bonus, _ = _bonus_and_record_id(s, emp_id)
    _delete_record(sf)
    assert base_bonus > 1000

    with sf() as s:
        s.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=1000,
            )
        )
        s.commit()

    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with sf() as s:
        bonus1, _ = _bonus_and_record_id(s, emp_id)
    assert base_bonus - bonus1 == 1000

    # 重算（不刪 record）
    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with sf() as s:
        bonus2, _ = _bonus_and_record_id(s, emp_id)
    assert bonus2 == bonus1, f"bulk 重算還原懲處：b1={bonus1} b2={bonus2}"


# ─────────────────────────────────────────────────────────────────────────────
# P2-D：ledger applied_amount 須等於實際扣薪金額（用 raw 可用額，非扣減後）
# ─────────────────────────────────────────────────────────────────────────────
def test_mark_applied_records_full_deduction(salary_engine_db):
    engine, sf = salary_engine_db
    emp_id = _setup_teacher(sf)

    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with sf() as s:
        base_bonus, _ = _bonus_and_record_id(s, emp_id)
    _delete_record(sf)

    # D > base_bonus/2，使「扣減後可用額」< D → 觸發 ledger 截斷 bug
    D = int(base_bonus) - 1
    assert D > base_bonus / 2, f"前置：D={D} 須 > base/2={base_bonus/2}"

    with sf() as s:
        s.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=D,
            )
        )
        s.commit()

    engine.process_bulk_salary_calculation([emp_id], 2026, 2)
    with sf() as s:
        new_bonus, _ = _bonus_and_record_id(s, emp_id)
        action = s.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        applied_amount = float(action.applied_amount or 0)

    actual_reduction = base_bonus - new_bonus
    assert actual_reduction == D, f"實際扣薪 {actual_reduction} 應 == D={D}"
    assert applied_amount == actual_reduction, (
        f"ledger applied_amount={applied_amount} 與實際扣薪 {actual_reduction} 不符"
        f"（用扣減後可用額截斷）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# C#1：engine 載入 BonusConfig 後須設 self._bonus_config，懲處預設金額用 admin 值
# ─────────────────────────────────────────────────────────────────────────────
def test_engine_applies_bonus_config_for_discipline_default(salary_engine_db):
    engine, sf = salary_engine_db
    with sf() as s:
        s.add(
            BonusConfig(
                config_year=2026,
                version=1,
                warning_deduction=1500.0,
                minor_offense_deduction=3500.0,
                major_offense_deduction=0.0,
            )
        )
        s.commit()

    with sf() as s:
        with engine.config_for_month(s, 2026, 2):
            cfg = getattr(engine, "_bonus_config", None)
            assert (
                cfg is not None
            ), "engine 載入 BonusConfig 後未賦值 self._bonus_config"
            assert float(cfg.warning_deduction) == 1500.0

            from services.disciplinary import _effective_amount

            a = DisciplinaryAction(
                employee_id=1,
                action_date=date(2026, 1, 1),
                action_type="warning",
                deduction_amount=0,  # 依賴 BonusConfig 預設
            )
            assert _effective_amount(a, cfg) == 1500.0, (
                "deduction_amount=0 的 warning 未採 BonusConfig.warning_deduction=1500"
                "（仍走 hardcode 1000）"
            )
