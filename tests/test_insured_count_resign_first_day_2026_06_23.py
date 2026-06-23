"""投保人數統計：當月 1 號離職的員工仍應計入該月（off-by-one 修正）。

qa-loop #11（2026-06-23）：get_insured_employee_count_by_month 用
`resign_date > month_first`，當 resign_date 正好等於該月 1 號時排除該員工，但其當月
仍投保至少一天；且與全 codebase「當月在職」慣例（`resign_date >= month_start`，見
api/gov_reports.py / api/salary）及 hire 端 `hire_date < month_end_exclusive`（含當月入職）
不對稱、與 docstring 自述「含當月離職」矛盾。改為 `resign_date >= month_first`。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from models.database import Employee
from services.finance_report_service import get_insured_employee_count_by_month


def _emp(s, eid, hire, resign, insured=Decimal("30000")):
    e = Employee(
        employee_id=eid,
        name=eid,
        hire_date=hire,
        resign_date=resign,
        labor_insured_salary=insured,
        is_active=resign is None,
    )
    s.add(e)
    s.flush()
    return e


def test_resign_on_month_first_still_counted_that_month(test_db_session):
    s = test_db_session
    _emp(s, "R1", date(2025, 1, 1), date(2026, 6, 1))  # 6/1 離職
    s.commit()
    out = get_insured_employee_count_by_month(s, 2026)
    assert out[6] == 1, "6/1 離職者當月仍投保至少一天，應計入 6 月（off-by-one）"
    assert out[7] == 0, "6/1 離職者不應計入 7 月"


def test_resign_prev_month_last_day_not_counted(test_db_session):
    s = test_db_session
    _emp(s, "R2", date(2025, 1, 1), date(2026, 5, 31))  # 5/31 離職
    s.commit()
    out = get_insured_employee_count_by_month(s, 2026)
    assert out[5] == 1
    assert out[6] == 0, "5/31 離職者 6 月已不在職，不應計入"
