"""補休 grant ledger ORM model 測試"""

from datetime import date

from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant


def test_grant_default_status_active():
    """新建 grant 預設為 active，已用 0、未過期、未綁帳"""
    g = OvertimeCompLeaveGrant(
        overtime_record_id=1,
        employee_id=2,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
    )
    assert g.status == "active"
    assert g.consumed_hours == 0.0
    assert g.expired_at is None
    assert g.payout_log_id is None
