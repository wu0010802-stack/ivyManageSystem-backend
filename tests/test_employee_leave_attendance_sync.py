"""sync service unit tests U-1~U-15"""

import pytest
from datetime import date, time
from decimal import Decimal

from services import employee_leave_attendance_sync as sync


class TestExceptions:
    def test_leave_attendance_conflict_is_exception(self):
        assert issubclass(sync.LeaveAttendanceConflict, Exception)

    def test_leave_not_approved_is_value_error(self):
        assert issubclass(sync.LeaveNotApproved, ValueError)

    def test_leave_partial_time_missing_is_value_error(self):
        assert issubclass(sync.LeavePartialTimeMissing, ValueError)


class TestIsFullDay:
    def test_full_day_when_no_times_and_hours_8(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        assert sync._is_full_day(leave) is True

    def test_full_day_when_no_times_and_hours_none(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=None)
        assert sync._is_full_day(leave) is True

    def test_not_full_day_when_start_time_set(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        assert sync._is_full_day(leave) is False

    def test_not_full_day_when_hours_lt_8(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=3.0)
        assert sync._is_full_day(leave) is False


class TestAssertLeaveTimeConsistent:
    def test_full_day_passes(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        sync._assert_leave_time_consistent(leave)  # 不該 raise

    def test_partial_with_times_passes(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        sync._assert_leave_time_consistent(leave)

    def test_partial_without_start_time_raises(self):
        leave = make_leave(start_time=None, end_time="12:00", leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)

    def test_partial_without_end_time_raises(self):
        leave = make_leave(start_time="09:00", end_time=None, leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)


# Helper:module-level stub,模擬 LeaveRecord 必要欄位
class _FakeLeave:
    """測試專用 stub,模擬 LeaveRecord 必要欄位。"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.employee_id = kwargs.get("employee_id", 1)
        self.start_date = kwargs.get("start_date", date(2026, 5, 22))
        self.end_date = kwargs.get("end_date", date(2026, 5, 22))
        self.start_time = kwargs.get("start_time")
        self.end_time = kwargs.get("end_time")
        self.leave_hours = kwargs.get("leave_hours", 8.0)
        self.leave_type = kwargs.get("leave_type", "personal")
        self.is_approved = kwargs.get("is_approved", True)


def make_leave(**kwargs):
    return _FakeLeave(**kwargs)
