"""年終 special_bonus_total 計算：excel 匯入列勝出、排除同型 auto-derive 列（不雙計）。

P1#2（qa-loop 全掃）：auto-derive 的 upsert uq 鍵含 per-class period_label（FESTIVAL_DIFF
'{yr}-FD'、AFTER_CLASS '{yr}上-C{cid}'、SEMESTER 同），與 Excel 匯入同 bonus_type 用固定
period_label（'114上'/'114.8-115.01'）不同。build_settlements 固定 refresh_rates=True →
derive_all 對 DRAFT 冪等「重建」auto 列；重建後同一 bonus_type 同時存在 Excel 列與 auto 列，
settlement_builder 以 SUM(全部 SpecialBonusItem.amount) 計 special_bonus_total → total_amount
（轉帳/發放）多發。

業主裁示「excel 最終真相」：compute_special_bonus_total_by_emp 對每個 (emp, bonus_type)，
若存在 excel 來源列（source_ref=='年終獎金總表'）則排除同型 auto 列（source_ref 以 'auto:' 開頭）。
無 excel 列的 type（純 auto/純手動）全計 → import 前一般 build 行為不變。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from services.year_end.settlement_builder import compute_special_bonus_total_by_emp


def _cycle(s, year: int = 114) -> YearEndCycle:
    c = YearEndCycle(
        academic_year=year,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        bonus_calc_date=date(2026, 1, 15),
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


def test_excel_wins_over_auto_same_bonus_type(test_db_session):
    s = test_db_session
    c = _cycle(s)
    EMP = 31
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114.8-115.01",
        1000,
        "年終獎金總表",
    )
    s.commit()
    totals = compute_special_bonus_total_by_emp(s, c.id)
    assert totals[EMP] == Decimal(
        "1000"
    ), f"excel 列應勝出、同型 auto 列排除，只計一次 1000；實際 {totals.get(EMP)}（2000=雙計）"


def test_auto_only_still_counted(test_db_session):
    s = test_db_session
    c = _cycle(s)
    EMP = 32
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    s.commit()
    assert compute_special_bonus_total_by_emp(s, c.id)[EMP] == Decimal("1000")


def test_per_class_auto_excluded_when_excel_aggregate_exists(test_db_session):
    s = test_db_session
    c = _cycle(s)
    EMP = 33
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.AFTER_CLASS_AWARD,
        "114上-C1",
        500,
        "auto:after_class_award",
    )
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.AFTER_CLASS_AWARD,
        "114上-C2",
        500,
        "auto:after_class_award",
    )
    _item(
        s, c.id, EMP, SpecialBonusType.AFTER_CLASS_AWARD, "114上", 1000, "年終獎金總表"
    )
    s.commit()
    assert compute_special_bonus_total_by_emp(s, c.id)[EMP] == Decimal(
        "1000"
    ), "excel 聚合列勝出、排除 per-class auto 列 → 1000，非 2000"


def test_different_bonus_types_independent(test_db_session):
    s = test_db_session
    c = _cycle(s)
    EMP = 34
    # FESTIVAL 有 excel → excel(800) 勝、auto(1000) 排除；SEMESTER 純 auto(300) → 全計 → 1100
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114.8-115.01",
        800,
        "年終獎金總表",
    )
    _item(
        s,
        c.id,
        EMP,
        SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
        "114上-C1",
        300,
        "auto:semester_dividend",
    )
    s.commit()
    assert compute_special_bonus_total_by_emp(s, c.id)[EMP] == Decimal("1100")
