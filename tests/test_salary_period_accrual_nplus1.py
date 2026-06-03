"""F3 回歸測試：發放月「期間累積」批次路徑不得逐員工解析班級 / 查會議缺席。

Bug（2026-06-02 修補前）：process_bulk_salary_calculation 的發放月期間累積
（_compute_period_accrual_totals）只預載了 school_active / cls_count_map，
未預載 classroom_for_emp 與 meeting_absent_count_map，導致每員工×每期間月仍
逐筆呼叫 _resolve_classroom_for_employee_in_term 與 MeetingRecord.count()
（N×期間月 的 N+1，落在最重的結算月 2/6/9/12）。

當月（非發放月）路徑早已由 _bulk_preload_for_salary_month 批次化；本測試守住
發放月期間累積路徑也不得退化成逐員工查詢，且批次班級解析與逐筆 resolver 同值。
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

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
    MeetingRecord,
    SalaryRecord,
    Student,
)
from services.salary_engine import SalaryEngine

# Feb 2026 發放月的期間月為 Dec 2025 + Jan 2026，皆對應學期 (114, 1)
PERIOD_SCHOOL_YEAR = 114
PERIOD_SEMESTER = 1


@pytest.fixture
def salary_engine_db(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'period-nplus1.sqlite'}",
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


def _teacher(session, emp_id, name):
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
    """建立對應期間學期 (114, 1) 的班級，讓批次班級解析能命中（非 fallback）。"""
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
                enrollment_date=date(2024, 8, 1),  # 早於 Dec/Jan period
                is_active=True,
            )
        )


def _setup_two_teachers(session):
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()
    a = _teacher(session, "PNP_A", "甲老師")
    b = _teacher(session, "PNP_B", "乙老師")
    ca = _term_classroom(session, "甲班", grade.id, a.id)
    cb = _term_classroom(session, "乙班", grade.id, b.id)
    a.classroom_id = ca.id
    b.classroom_id = cb.id
    _students(session, ca.id, 27, "AA")
    _students(session, cb.id, 27, "BB")
    session.commit()
    return a.id, b.id


def test_bulk_period_accrual_does_not_resolve_classroom_per_employee(salary_engine_db):
    """發放月 bulk：期間累積不得逐員工呼叫 _resolve_classroom_for_employee_in_term。

    班級已建於期間學期 (114,1) → 批次預載應命中 classroom_for_emp，逐筆 resolver
    呼叫次數應為 0（非 fallback 員工不應 fall through）。
    """
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        a_id, b_id = _setup_two_teachers(s)

    spy = MagicMock(wraps=engine._resolve_classroom_for_employee_in_term)
    engine._resolve_classroom_for_employee_in_term = spy

    engine.process_bulk_salary_calculation([a_id, b_id], 2026, 2)

    assert (
        spy.call_count == 0
    ), f"發放月期間累積仍逐員工解析班級 {spy.call_count} 次（N+1 未消除）"


def test_single_vs_bulk_february_parity_with_term_classrooms(salary_engine_db):
    """批次班級解析須與逐筆 resolver 同值：single 與 bulk 的節慶/超額逐員工相同。

    用期間學期 (114,1) 班級，確保 bulk 走「批次 classroom_for_emp」、single 走
    「逐筆 _resolve_classroom_for_employee_in_term」，兩路徑必須一致。
    """
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        a_id, b_id = _setup_two_teachers(s)

    def _snap(emp_id):
        with session_factory() as s:
            r = (
                s.query(SalaryRecord)
                .filter(
                    SalaryRecord.employee_id == emp_id,
                    SalaryRecord.salary_year == 2026,
                    SalaryRecord.salary_month == 2,
                )
                .one()
            )
            return (float(r.festival_bonus or 0), float(r.overtime_bonus or 0))

    # 單筆路徑
    engine.process_salary_calculation(a_id, 2026, 2)
    engine.process_salary_calculation(b_id, 2026, 2)
    single_a, single_b = _snap(a_id), _snap(b_id)

    with session_factory() as s:
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == 2026, SalaryRecord.salary_month == 2
        ).delete()
        s.commit()

    # 批次路徑
    engine.process_bulk_salary_calculation([a_id, b_id], 2026, 2)
    bulk_a, bulk_b = _snap(a_id), _snap(b_id)

    assert single_a == bulk_a, f"甲 single={single_a} bulk={bulk_a}"
    assert single_b == bulk_b, f"乙 single={single_b} bulk={bulk_b}"
    # 確保獎金確實 > 0（否則 parity 被零值掩蓋，沒測到班級解析路徑）
    assert bulk_a[0] > 0


def test_build_period_monthly_context_preloads_meeting_and_classroom(salary_engine_db):
    """_build_period_monthly_context 應為每個期間月預載 meeting_absent_count_map
    與 classroom_for_emp，供下游跳過逐員工查詢。"""
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        a_id, b_id = _setup_two_teachers(s)
        # 甲在 Dec 2025 有一次會議缺席
        s.add(
            MeetingRecord(
                employee_id=a_id,
                meeting_date=date(2025, 12, 10),
                attended=False,
            )
        )
        s.commit()

    with session_factory() as s:
        classroom_map = {c.id: c for c in s.query(Classroom).all()}
        cache = engine._build_period_monthly_context(
            s, 2026, 2, classroom_map, [a_id, b_id]
        )

    # 期間月 (2025,12) 與 (2026,1) 皆應有兩個新預載 key
    for key in [(2025, 12), (2026, 1)]:
        assert key in cache
        assert "meeting_absent_count_map" in cache[key]
        assert "classroom_for_emp" in cache[key]
        # 兩位老師的班級都在 (114,1) → 應被批次解析命中
        assert cache[key]["classroom_for_emp"].get(a_id) is not None
        assert cache[key]["classroom_for_emp"].get(b_id) is not None

    # 甲 Dec 缺席 1 次；Jan 無 → Dec map 應含甲，Jan 不含
    assert cache[(2025, 12)]["meeting_absent_count_map"].get(a_id) == 1
    assert cache[(2026, 1)]["meeting_absent_count_map"].get(a_id, 0) == 0
