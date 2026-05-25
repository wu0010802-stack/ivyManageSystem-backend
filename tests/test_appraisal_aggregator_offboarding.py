"""aggregate_all_active_employees_status 離職員工 filter 測試（Task 12）。

驗證 cycle 期間離職的員工應出現在彙整結果，
cycle 前已離職者不應出現。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.appraisal import (
    AppraisalCycle,
    CycleStatus,
    Semester,
)
from models.employee import Employee, EmployeeType
from services.appraisal.status_aggregator import (
    aggregate_all_active_employees_status,
)

# ===== helpers =====


def _make_employee(
    session,
    name: str,
    eid: str,
    *,
    is_active: bool = True,
    resign_date: date | None = None,
) -> Employee:
    emp = Employee(
        employee_id=eid,
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=is_active,
        resign_date=resign_date,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_cycle(session) -> AppraisalCycle:
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()
    return cycle


# ===== tests =====


def test_includes_employee_who_resigned_within_cycle(test_db_session):
    """cycle 期間離職的員工（resign_date 在 cycle 範圍內）應出現在彙整結果；
    cycle 前已離職（resign_date < cycle.start_date）的員工不應出現；
    仍在職（is_active=True）的員工也應出現。
    """
    s = test_db_session
    cycle = _make_cycle(s)

    # 仍在職
    emp_active = _make_employee(s, "在職員工", "OFB001", is_active=True)

    # cycle 中途離職（resign_date 在 cycle 範圍內）
    emp_resigned_during = _make_employee(
        s,
        "cycle 中離職",
        "OFB002",
        is_active=False,
        resign_date=date(2025, 10, 15),  # cycle 2025-08-01 ~ 2026-01-31 內
    )

    # cycle 開始前已離職
    emp_resigned_before = _make_employee(
        s,
        "cycle 前已離",
        "OFB003",
        is_active=False,
        resign_date=date(2025, 7, 31),  # cycle start_date 前一天
    )

    s.commit()

    out = aggregate_all_active_employees_status(s, cycle)
    emp_ids = {row.employee_id for row in out}

    assert emp_active.id in emp_ids, "在職員工應出現"
    assert emp_resigned_during.id in emp_ids, "cycle 期間離職員工應出現"
    assert emp_resigned_before.id not in emp_ids, "cycle 前已離職員工不應出現"


def test_existing_active_employees_still_included(test_db_session):
    """保證原本 is_active=True 的員工在 filter 改動後仍正常出現（回歸保護）。"""
    s = test_db_session
    cycle = _make_cycle(s)

    emp_a = _make_employee(s, "在職A", "OFB010", is_active=True)
    emp_b = _make_employee(s, "在職B", "OFB011", is_active=True)
    s.commit()

    out = aggregate_all_active_employees_status(s, cycle)
    emp_ids = {row.employee_id for row in out}

    assert emp_a.id in emp_ids, "在職員工 A 應出現"
    assert emp_b.id in emp_ids, "在職員工 B 應出現"
    assert len(out) == 2
