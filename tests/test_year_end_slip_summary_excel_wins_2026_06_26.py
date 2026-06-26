"""年終獎金條 PDF／年終總表「分項」彙總須套 excel-wins 去重，與 total_amount 對帳一致。

qa-loop P2#1（2026-06-26 全掃）：轉帳金額 `YearEndSettlement.total_amount` 由
`compute_special_bonus_total_by_emp` 做 excel-wins 去重（同 (emp, bonus_type) 同時有 Excel 列
與 auto 列時只計 Excel 列），匯款正確。但個人獎金條 PDF 走的 `_aggregate_bonus_by_type` 與
年終總表 `_build_summary_rows` 的 `bonus_by_type` 是無條件 `current + amount` 加總、未去重 →
Excel 匯入後 auto 列與 Excel 列並存時，員工面分項被雙計、獎金條總額 > 實際入帳 total_amount，
年終總表 per-type 欄也對不上 total 欄。

修法：兩處分項彙總改走同一 excel-wins 去重口徑（compute_special_bonus_by_type_by_emp），
使「Σ 分項 == special_bonus_total == total_amount − payable」恆成立。
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

from api.year_end import _aggregate_bonus_by_type, _build_summary_rows
from models.database import Base
from models.employee import Employee, EmployeeType
from models.year_end import (
    EmployeeYearEndSnapshot,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.year_end.settlement_builder import compute_special_bonus_total_by_emp


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ye-slip-summary.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _cycle(s) -> YearEndCycle:
    c = YearEndCycle(
        academic_year=114,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
        status=YearEndCycleStatus.OPEN,
    )
    s.add(c)
    s.flush()
    return c


def _item(s, cycle_id, emp_id, btype, label, amount, source_ref):
    s.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_id,
            employee_id=emp_id,
            bonus_type=btype,
            period_label=label,
            amount=Decimal(str(amount)),
            source_ref=source_ref,
        )
    )


def _emp(s, name) -> Employee:
    e = Employee(
        employee_id=f"E-{name}",
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    s.add(e)
    s.flush()
    return e


def test_aggregate_bonus_by_type_applies_excel_wins(sf):
    """slip PDF 路徑：同型 Excel+auto 並存只計 Excel 列（1000），非雙計 2000。"""
    with sf() as s:
        c = _cycle(s)
        emp = _emp(s, "班導")
        # FESTIVAL_DIFF：auto 列 + Excel 聚合列並存（build refresh_rates 後預期狀態）
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114-FD",
            1000,
            "auto:festival_diff",
        )
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114.8-115.01",
            1000,
            "年終獎金總表",
        )
        s.commit()
        cid, eid = c.id, emp.id

    with sf() as s:
        by_type = _aggregate_bonus_by_type(s, cid)

    assert by_type[eid][SpecialBonusType.FESTIVAL_DIFF] == Decimal("1000"), (
        f"獎金條分項應套 excel-wins 去重只計 1000，實際 "
        f"{by_type[eid].get(SpecialBonusType.FESTIVAL_DIFF)}（2000=雙計）"
    )


def test_aggregate_reconciles_with_transfer_total(sf):
    """恆等式：Σ 獎金條分項 == compute_special_bonus_total_by_emp（轉帳口徑）。"""
    with sf() as s:
        c = _cycle(s)
        emp = _emp(s, "班導2")
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114-FD",
            1000,
            "auto:festival_diff",
        )
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114.8-115.01",
            1000,
            "年終獎金總表",
        )
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.AFTER_CLASS_AWARD,
            "114上-C1",
            500,
            "auto:after_class_award",
        )
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.AFTER_CLASS_AWARD,
            "114上",
            500,
            "年終獎金總表",
        )
        # 純 auto（無 Excel 列）→ 全計
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            "114上-C1",
            300,
            "auto:semester_dividend",
        )
        s.commit()
        cid, eid = c.id, emp.id

    with sf() as s:
        by_type = _aggregate_bonus_by_type(s, cid)
        transfer_total = compute_special_bonus_total_by_emp(s, cid)

    slip_sum = sum(by_type[eid].values(), Decimal("0"))
    assert slip_sum == transfer_total[eid], (
        f"獎金條分項加總 {slip_sum} 必須等於轉帳口徑 {transfer_total[eid]}"
        "（1000+500+300=1800，非雙計 2000+1000+300=3300）"
    )
    assert slip_sum == Decimal("1800")


def test_build_summary_rows_bonus_by_type_deduped(sf):
    """年終總表：per-type 分項去重後，Σ 分項 == total − year_end_amount（payable）。"""
    with sf() as s:
        c = _cycle(s)
        emp = _emp(s, "班導3")
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114-FD",
            1000,
            "auto:festival_diff",
        )
        _item(
            s,
            c.id,
            emp.id,
            SpecialBonusType.FESTIVAL_DIFF,
            "114.8-115.01",
            1000,
            "年終獎金總表",
        )
        snap = EmployeeYearEndSnapshot(
            year_end_cycle_id=c.id,
            employee_id=emp.id,
            base_salary=Decimal("0"),
            festival_total=Decimal("0"),
            hire_months=Decimal("12"),
        )
        s.add(snap)
        s.flush()
        # 轉帳口徑：special_bonus_total = 1000（去重後），total = payable + 1000
        s.add(
            YearEndSettlement(
                year_end_cycle_id=c.id,
                employee_id=emp.id,
                snapshot_id=snap.id,
                payable_amount=Decimal("50000"),
                total_amount=Decimal("51000"),
                special_bonus_total=Decimal("1000"),
                status=YearEndSettlementStatus.DRAFT,
            )
        )
        s.commit()
        cid, eid = c.id, emp.id

    with sf() as s:
        rows = _build_summary_rows(s, cid)

    row = next(r for r in rows if r.name == "班導3")
    bonus_sum = sum(row.bonus_by_type.values(), Decimal("0"))
    assert bonus_sum == row.total - row.year_end_amount == Decimal("1000"), (
        f"總表分項加總 {bonus_sum} 應 == total − payable = "
        f"{row.total - row.year_end_amount}（去重後 1000，非雙計 2000）"
    )
