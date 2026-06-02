"""BE-P2-1：single vs bulk 薪資路徑「值完全相同」整合 parity 測試。

batch==single 單元測試證明每個 helper 算對；本測試證明 bulk 迴圈把「對的值接到對的
員工的對的欄位」——這正是效能重構最容易悄悄改錯薪資的地方。

兩位員工刻意給「不同」的 appraisal 與 skip 狀態：若 bulk 把 A 的值接到 B（交叉接線），
single==bulk 比對就會失敗。
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
    Employee,
    LeaveRecord,
    SalaryRecord,
    Student,
)
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from services.salary_engine import SalaryEngine


@pytest.fixture
def salary_engine_db(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'bulk-parity.sqlite'}",
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


def _teacher(session, emp_id, name, hire_date=date(2024, 1, 1)):
    t = Employee(
        employee_id=emp_id,
        name=name,
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=hire_date,
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


_FIELDS = (
    "appraisal_year_end_bonus",
    "festival_bonus",
    "overtime_bonus",
    "health_insurance_employee",
    "supplementary_health_employee",
    "unused_leave_payout",
    "net_salary",
)


def _snapshot(session, emp_id):
    r = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == emp_id,
            SalaryRecord.salary_year == 2026,
            SalaryRecord.salary_month == 2,
        )
        .one()
    )
    return {f: getattr(r, f) for f in _FIELDS}


def test_single_vs_bulk_february_parity_distinct_values(salary_engine_db):
    """Feb：A（正常、appraisal 13600）vs B（產假橫跨 Dec+Jan、appraisal 8000）。
    single 與 bulk 兩路徑對每位員工的薪資欄位必須完全相同，且兩人值彼此不同
    （否則交叉接線 bug 會被同值掩蓋）。"""
    engine, session_factory = salary_engine_db

    with session_factory() as s:
        grade = ClassGrade(name="大班", is_active=True)
        s.add(grade)
        s.flush()

        a = _teacher(s, "PAR_A", "甲老師")
        b = _teacher(s, "PAR_B", "乙老師")
        ca = Classroom(
            name="甲班", grade_id=grade.id, head_teacher_id=a.id, is_active=True
        )
        cb = Classroom(
            name="乙班", grade_id=grade.id, head_teacher_id=b.id, is_active=True
        )
        s.add_all([ca, cb])
        s.flush()
        a.classroom_id = ca.id
        b.classroom_id = cb.id
        _students(s, ca.id, 27, "AA")
        _students(s, cb.id, 27, "BB")

        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
        )
        s.add(cycle)
        s.flush()
        # A 考核 13600；B 考核 8000（刻意不同）
        s.add_all(
            [
                SpecialBonusItem(
                    year_end_cycle_id=cycle.id,
                    employee_id=a.id,
                    bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    period_label="113下",
                    amount=Decimal("6600"),
                ),
                SpecialBonusItem(
                    year_end_cycle_id=cycle.id,
                    employee_id=a.id,
                    bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                    period_label="114上",
                    amount=Decimal("7000"),
                ),
                SpecialBonusItem(
                    year_end_cycle_id=cycle.id,
                    employee_id=b.id,
                    bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    period_label="113下",
                    amount=Decimal("8000"),
                ),
            ]
        )
        # B 產假橫跨 Dec 2025 + Jan 2026（Feb 的兩個 period 月）→ 兩月皆 skip 獎金
        s.add(
            LeaveRecord(
                employee_id=b.id,
                leave_type="maternity",
                start_date=date(2025, 12, 10),
                end_date=date(2026, 1, 20),
                leave_hours=0,
                status="approved",
            )
        )
        # A 有 Feb pending 折現 log 5000；B 無（pending_payout 路徑差異化）
        from models.unused_leave_payout_log import UnusedLeavePayoutLog

        s.add(
            UnusedLeavePayoutLog(
                employee_id=a.id,
                source_type="comp_leave_expiry",
                hours=8,
                hourly_wage=Decimal("625"),
                amount=Decimal("5000"),
                wage_basis_date=date(2026, 2, 1),
                salary_period_year=2026,
                salary_period_month=2,
            )
        )
        s.commit()
        a_id, b_id = a.id, b.id

    # ── 單筆路徑 ──
    engine.process_salary_calculation(a_id, 2026, 2)
    engine.process_salary_calculation(b_id, 2026, 2)
    with session_factory() as s:
        single_a = _snapshot(s, a_id)
        single_b = _snapshot(s, b_id)

    # 清掉單筆寫入的 Feb 記錄，並把單筆已綁定的 pending log 還原為未綁定，
    # 讓 bulk 在乾淨狀態重算（pending_payout 的 idempotent filter 是 salary_record_id IS NULL）。
    with session_factory() as s:
        from models.unused_leave_payout_log import UnusedLeavePayoutLog

        s.query(UnusedLeavePayoutLog).update(
            {UnusedLeavePayoutLog.salary_record_id: None}
        )
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == 2026, SalaryRecord.salary_month == 2
        ).delete()
        s.commit()

    # ── 批次路徑 ──
    engine.process_bulk_salary_calculation([a_id, b_id], 2026, 2)
    with session_factory() as s:
        bulk_a = _snapshot(s, a_id)
        bulk_b = _snapshot(s, b_id)

    # 核心：single == bulk（逐欄位、逐員工）
    assert single_a == bulk_a, f"A 路徑不一致 single={single_a} bulk={bulk_a}"
    assert single_b == bulk_b, f"B 路徑不一致 single={single_b} bulk={bulk_b}"

    # 決策⑥B：engine 不再 pull 考核獎金（由年終模組獨立發放），兩人皆 0
    assert bulk_a["appraisal_year_end_bonus"] == 0
    assert bulk_b["appraisal_year_end_bonus"] == 0
    # skip 生效：B 產假橫跨兩 period 月 → festival/overtime 歸 0；A 正常 > 0
    assert bulk_a["festival_bonus"] > 0
    assert bulk_b["festival_bonus"] == 0
    assert bulk_b["overtime_bonus"] == 0
    # pending_payout 生效且差異化：A 折現 5000；B 無
    assert bulk_a["unused_leave_payout"] == Decimal("5000")
    assert not bulk_b["unused_leave_payout"]


def test_bulk_does_not_invoke_per_employee_n_plus_1(salary_engine_db, monkeypatch):
    """bulk 路徑不得再逐人呼叫 per-employee 版（appraisal/ytd/skip）→ 證明 N+1 已由
    Phase 1 batch 預載取代（appraisal 雙查也一併消除）。spy 以 wraps 包原函式，
    行為不變只計次。"""
    from unittest.mock import MagicMock

    import services.leave_bonus_skip as skip_mod
    import services.salary.engine as eng_mod
    import services.salary.supplementary_premium as sup_mod
    from services.leave_bonus_skip import should_skip_bonuses_for_month as _skip
    from services.salary.appraisal_year_end import query_appraisal_year_end_bonus as _ap
    from services.salary.supplementary_premium import query_ytd_bonus_before as _ytd

    engine, session_factory = salary_engine_db
    with session_factory() as s:
        grade = ClassGrade(name="大班", is_active=True)
        s.add(grade)
        s.flush()
        a = _teacher(s, "NP_A", "甲")
        b = _teacher(s, "NP_B", "乙")
        ca = Classroom(
            name="甲", grade_id=grade.id, head_teacher_id=a.id, is_active=True
        )
        cb = Classroom(
            name="乙", grade_id=grade.id, head_teacher_id=b.id, is_active=True
        )
        s.add_all([ca, cb])
        s.flush()
        a.classroom_id, b.classroom_id = ca.id, cb.id
        _students(s, ca.id, 25, "NPA")
        _students(s, cb.id, 25, "NPB")
        s.commit()
        ids = [a.id, b.id]

    spies = {
        # engine.query_appraisal removed: 決策⑥B deleted the function from engine.py
        "sup.query_appraisal": MagicMock(wraps=_ap),
        "sup.query_ytd_before": MagicMock(wraps=_ytd),
        "skip.should_skip_for_month": MagicMock(wraps=_skip),
    }
    # 決策⑥B：engine.py の query_appraisal_year_end_bonus 已刪除，無需 spy
    monkeypatch.setattr(
        sup_mod, "query_appraisal_year_end_bonus", spies["sup.query_appraisal"]
    )
    monkeypatch.setattr(
        sup_mod, "query_ytd_bonus_before", spies["sup.query_ytd_before"]
    )
    monkeypatch.setattr(
        skip_mod, "should_skip_bonuses_for_month", spies["skip.should_skip_for_month"]
    )

    engine.process_bulk_salary_calculation(ids, 2026, 2)

    for label, spy in spies.items():
        assert (
            spy.call_count == 0
        ), f"{label} 仍被逐人呼叫 {spy.call_count} 次（N+1 未消除）"
