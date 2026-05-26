"""UnusedLeavePayoutLog model 單元測試。"""

from datetime import date
from decimal import Decimal

from models.unused_leave_payout_log import UnusedLeavePayoutLog


def test_unused_leave_payout_log_columns():
    log = UnusedLeavePayoutLog(
        employee_id=1,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        meta={"expired_grant_ids": [123]},
    )
    assert log.source_type == "comp_grant_expiry"
    assert log.amount == Decimal("800.00")
    assert log.meta["expired_grant_ids"] == [123]
    assert log.salary_record_id is None
