"""compute_attendance_dates 純函式單元測試（不碰 DB）。

涵蓋：
- 週末排除
- 國定假日排除
- 補班日保留
- 起迄同日
- 跨月
- 純工作日連續
- 起迄反轉拋 ValueError
"""

from datetime import date

import pytest

from services.student_leave_service import (
    compute_attendance_dates,
    is_remark_owned_by_leave,
    make_remark,
)


class TestComputeAttendanceDates:
    def test_pure_workdays(self):
        # 2026-04-20 (一) ~ 2026-04-24 (五)
        result = compute_attendance_dates(
            date(2026, 4, 20),
            date(2026, 4, 24),
            holiday_map={},
            makeup_map={},
        )
        assert result == [
            date(2026, 4, 20),
            date(2026, 4, 21),
            date(2026, 4, 22),
            date(2026, 4, 23),
            date(2026, 4, 24),
        ]

    def test_excludes_weekends(self):
        # 2026-04-24 (五) ~ 2026-04-27 (一)
        result = compute_attendance_dates(
            date(2026, 4, 24),
            date(2026, 4, 27),
            holiday_map={},
            makeup_map={},
        )
        assert result == [date(2026, 4, 24), date(2026, 4, 27)]

    def test_excludes_holidays(self):
        # 假設 2026-04-22 (三) 為國定假日
        result = compute_attendance_dates(
            date(2026, 4, 20),
            date(2026, 4, 24),
            holiday_map={date(2026, 4, 22): "假設假日"},
            makeup_map={},
        )
        assert date(2026, 4, 22) not in result
        assert date(2026, 4, 21) in result
        assert date(2026, 4, 23) in result

    def test_includes_makeup_weekend(self):
        # 2026-04-25 (六) 設為補班日
        result = compute_attendance_dates(
            date(2026, 4, 24),
            date(2026, 4, 27),
            holiday_map={},
            makeup_map={date(2026, 4, 25): "補上班日"},
        )
        assert date(2026, 4, 25) in result  # 補班週六應到
        assert date(2026, 4, 26) not in result  # 一般週日不到

    def test_makeup_overrides_holiday(self):
        """同一天既出現在 holiday_map 又在 makeup_map：以 makeup 為準（學生要到校）。"""
        result = compute_attendance_dates(
            date(2026, 4, 25),  # 週六
            date(2026, 4, 25),
            holiday_map={date(2026, 4, 25): "意外列入假日"},
            makeup_map={date(2026, 4, 25): "補班勝出"},
        )
        assert result == [date(2026, 4, 25)]

    def test_single_day_workday(self):
        result = compute_attendance_dates(
            date(2026, 4, 20),
            date(2026, 4, 20),
            holiday_map={},
            makeup_map={},
        )
        assert result == [date(2026, 4, 20)]

    def test_single_day_weekend_returns_empty(self):
        result = compute_attendance_dates(
            date(2026, 4, 25),  # 週六
            date(2026, 4, 25),
            holiday_map={},
            makeup_map={},
        )
        assert result == []

    def test_cross_month(self):
        # 2026-04-29 (三) ~ 2026-05-04 (一)
        result = compute_attendance_dates(
            date(2026, 4, 29),
            date(2026, 5, 4),
            holiday_map={date(2026, 5, 1): "勞動節"},
            makeup_map={},
        )
        assert date(2026, 4, 30) in result
        assert date(2026, 5, 1) not in result  # 勞動節
        assert date(2026, 5, 4) in result

    def test_invalid_range_raises(self):
        with pytest.raises(ValueError):
            compute_attendance_dates(
                date(2026, 4, 22),
                date(2026, 4, 20),
                holiday_map={},
                makeup_map={},
            )


class TestRemarkHelpers:
    def test_make_and_match(self):
        r = make_remark(42)
        assert "42" in r
        assert is_remark_owned_by_leave(r, 42)

    def test_no_match_when_different_id(self):
        r = make_remark(42)
        assert not is_remark_owned_by_leave(r, 43)

    def test_none_remark_not_owned(self):
        assert not is_remark_owned_by_leave(None, 1)

    def test_arbitrary_remark_not_owned(self):
        assert not is_remark_owned_by_leave("孩子不舒服", 1)
