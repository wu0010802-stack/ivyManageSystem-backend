"""
tests/test_kiosk_migration_columns.py — 驗證 kiosk 打卡 PIN 欄位與考勤來源欄位存在於 model
"""

from models.database import Employee, Attendance


def test_employee_has_punch_pin_columns():
    assert hasattr(Employee, "punch_pin_hash")
    assert hasattr(Employee, "punch_pin_set_at")


def test_attendance_has_source_column():
    assert hasattr(Attendance, "source")
