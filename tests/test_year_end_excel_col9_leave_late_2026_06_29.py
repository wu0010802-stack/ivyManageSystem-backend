"""年終 Excel 主表解析：請假遲到扣款須同時讀 col 8（114.02-114.12）與 col 9（115.01）。

QA loop（2026-06-29）：_parse_year_end_main_sheet 只讀 row[8]，docstring 明列的
col 9（row[9]=115.01 請假遲到）漏讀。import_year_end_to_db 把此值寫入
settlement.deduction_leave_late，build_settlements re-build 時 read-back 此欄當
leave_late_prev 重算 payable=(subtotal+deduction_total)×proration。col 9 非零時扣項偏小
→ deduction_total 較不負 → payable 變大 → 年終轉帳多發。

修法：leave_late = row[8] + row[9]。
"""

from __future__ import annotations

from decimal import Decimal

from services.year_end.excel_io import _parse_year_end_main_sheet


def _main_row(col8, col9, n_cols: int = 19):
    """構造一筆能通過 _is_summary_data_row + base_salary 主分支的合成 row。

    欄位：0 姓名 |1 base |2 festival |3 - |4 avg_perf |5 gross |6 org_rate
    |7 subtotal |8 114.02-114.12 請假遲到 |9 115.01 請假遲到 |10 奬懲 |11 會議
    |12 事假 |13 病假 |14 遲到早退 |15 合計 |16 到職(月) |17 應領小計 |18 備註
    """
    row = [
        "測試員工",
        36000,
        2000,
        0,
        95.0,
        38000,
        100,
        30000,
        col8,
        col9,
        0,
        0,
        0,
        0,
        0,
        0,
        12,
        29000,
        "",
    ]
    return row[:n_cols]


def test_leave_late_sums_col8_and_col9():
    """col 8 與 col 9 都是請假遲到扣款，deduction_leave_late 應為兩者之和。"""
    parsed = _parse_year_end_main_sheet([_main_row(col8=100, col9=200)])
    assert len(parsed) == 1
    assert parsed[0].deduction_leave_late == Decimal("300")


def test_leave_late_col9_only():
    """只有 col 9 非零（col 8 為 0）時仍須計入。"""
    parsed = _parse_year_end_main_sheet([_main_row(col8=0, col9=150)])
    assert parsed[0].deduction_leave_late == Decimal("150")


def test_leave_late_short_row_without_col9_is_safe():
    """row 短於 col 9（缺欄，僅到 index 8）時不應 IndexError，退回只計 col 8。"""
    parsed = _parse_year_end_main_sheet([_main_row(col8=100, col9=0, n_cols=9)])
    assert parsed[0].deduction_leave_late == Decimal("100")
