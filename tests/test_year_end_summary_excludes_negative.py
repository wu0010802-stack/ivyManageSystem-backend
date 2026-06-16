"""年終總表排除負值結算（2026-06-15 運作探測 P3-2）。

Bug：export_summary 列入所有 settlement（含時薪/扣款超基數產生的負 payable），
  但轉帳名冊只取 total_amount>0 → 總表合計 vs 名冊合計差「負值總和」、且總表
  曝出負年終誤導簽核者。
業主裁示：不改 engine（負值保留 DB），只修總表呈現＝排除負值列；保留 0 列
  （資訊性），使總表合計與名冊合計對齊。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.year_end import _build_summary_rows
from models.database import Base
from models.employee import Employee, EmployeeType
from models.year_end import (
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ye-summary.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _mk_settlement(s, cycle_id, name, amount):
    emp = Employee(
        employee_id=f"E-{name}",
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    s.add(emp)
    s.flush()
    snap = EmployeeYearEndSnapshot(
        year_end_cycle_id=cycle_id,
        employee_id=emp.id,
        base_salary=Decimal("0"),
        festival_total=Decimal("0"),
        hire_months=Decimal("12"),
    )
    s.add(snap)
    s.flush()
    s.add(
        YearEndSettlement(
            year_end_cycle_id=cycle_id,
            employee_id=emp.id,
            snapshot_id=snap.id,
            payable_amount=Decimal(amount),
            total_amount=Decimal(amount),
            special_bonus_total=Decimal("0"),
            status=YearEndSettlementStatus.DRAFT,
        )
    )
    s.flush()


def test_build_summary_rows_excludes_negative(sf):
    with sf() as s:
        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
            status=YearEndCycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        _mk_settlement(s, cycle.id, "正值", "50000")
        _mk_settlement(s, cycle.id, "零值", "0")
        _mk_settlement(s, cycle.id, "負值時薪", "-1250")
        s.commit()
        cid = cycle.id

    with sf() as s:
        rows = _build_summary_rows(s, cid)

    names = {r.name for r in rows}
    assert "正值" in names
    assert "零值" in names  # 0 列保留（資訊性）
    assert "負值時薪" not in names  # 負值排除（不誤導、與轉帳名冊對齊）
    assert sum((r.total for r in rows), Decimal("0")) == Decimal("50000")
