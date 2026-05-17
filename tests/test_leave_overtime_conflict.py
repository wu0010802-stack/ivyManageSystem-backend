"""tests/test_leave_overtime_conflict.py — utils/leave_overtime_conflict.py 純函式測試。"""

from datetime import datetime, time

import pytest

from utils.leave_overtime_conflict import times_overlap, to_time


class TestToTime:
    def test_str_hh_mm(self):
        assert to_time("08:30") == time(8, 30)

    def test_str_with_seconds_truncated(self):
        # 只取 HH:MM
        assert to_time("08:30:45") == time(8, 30)

    def test_str_with_whitespace(self):
        assert to_time("  09:15 ") == time(9, 15)

    def test_str_midnight(self):
        assert to_time("00:00") == time(0, 0)

    def test_time_object_passthrough(self):
        t = time(14, 25)
        assert to_time(t) == t

    def test_datetime_extracts_time(self):
        dt = datetime(2026, 5, 17, 16, 45)
        assert to_time(dt) == time(16, 45)

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError, match="無法將"):
            to_time(12345)

    def test_none_raises(self):
        with pytest.raises(TypeError, match="無法將"):
            to_time(None)

    def test_str_invalid_format_raises(self):
        # 沒有冒號 → split 後變單元素，int() 會 raise
        with pytest.raises(ValueError):
            to_time("abc")


class TestTimesOverlap:
    def test_full_overlap(self):
        assert times_overlap("08:00", "12:00", "09:00", "11:00") is True

    def test_partial_overlap_left(self):
        assert times_overlap("08:00", "10:00", "09:00", "11:00") is True

    def test_partial_overlap_right(self):
        assert times_overlap("10:00", "12:00", "09:00", "11:00") is True

    def test_no_overlap_before(self):
        assert times_overlap("08:00", "09:00", "10:00", "11:00") is False

    def test_no_overlap_after(self):
        assert times_overlap("12:00", "13:00", "10:00", "11:00") is False

    def test_touching_endpoint_not_overlap(self):
        # 端點相接（10:00 == 10:00）不視為重疊
        assert times_overlap("08:00", "10:00", "10:00", "12:00") is False

    def test_touching_endpoint_reversed_not_overlap(self):
        assert times_overlap("10:00", "12:00", "08:00", "10:00") is False

    def test_identical_ranges_overlap(self):
        assert times_overlap("09:00", "12:00", "09:00", "12:00") is True

    def test_mixed_types_str_and_time(self):
        # 一邊 str 一邊 time
        assert times_overlap("08:00", "10:00", time(9, 0), time(11, 0)) is True

    def test_mixed_types_str_and_datetime(self):
        # 一邊 str 一邊 datetime
        dt_start = datetime(2026, 5, 17, 9, 0)
        dt_end = datetime(2026, 5, 17, 11, 0)
        assert times_overlap("08:00", "10:00", dt_start, dt_end) is True

    def test_one_minute_overlap(self):
        # 1 分鐘重疊也算
        assert times_overlap("08:00", "10:01", "10:00", "12:00") is True
