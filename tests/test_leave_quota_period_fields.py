"""
tests/test_leave_quota_period_fields.py — LeaveQuota period_start/period_end 欄位測試
"""

from datetime import date

from models.leave import LeaveQuota


def test_leave_quota_period_fields_default_null():
    """period_start/period_end 預設為 NULL"""
    q = LeaveQuota(employee_id=1, year=2026, leave_type="annual", total_hours=80.0)
    assert q.period_start is None
    assert q.period_end is None


def test_leave_quota_period_fields_set():
    """period_start/period_end 可設定"""
    q = LeaveQuota(
        employee_id=1,
        year=2026,
        leave_type="annual",
        total_hours=80.0,
        period_start=date(2025, 8, 15),
        period_end=date(2026, 8, 15),
    )
    assert q.period_start == date(2025, 8, 15)
    assert q.period_end == date(2026, 8, 15)
