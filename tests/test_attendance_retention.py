"""出勤紀錄 5 年保存期限守衛（勞基法第 30 條第 5 項）

「雇主應置備勞工出勤紀錄，並保存五年。」
DELETE endpoint 在刪除前必須檢查日期，5 年內的紀錄不得刪除。
"""

from datetime import date

import pytest
from fastapi import HTTPException

from api.attendance.records import _assert_attendance_within_retention


class TestAttendanceRetentionGuard:
    def test_yesterday_in_retention_raises(self):
        """昨天的紀錄在保存期內 → 400"""
        today = date(2026, 4, 22)
        with pytest.raises(HTTPException) as exc:
            _assert_attendance_within_retention(date(2026, 4, 21), today=today)
        assert exc.value.status_code == 400
        assert "保存期" in exc.value.detail or "5 年" in exc.value.detail

    def test_exactly_5_years_ago_in_retention_raises(self):
        """剛好 5 年前的同一天，仍在保存期 → 400"""
        today = date(2026, 4, 22)
        with pytest.raises(HTTPException):
            _assert_attendance_within_retention(date(2021, 4, 22), today=today)

    def test_5_years_and_1_day_ago_can_delete(self):
        """5 年又 1 天前 → 已逾保存期，允許刪除"""
        today = date(2026, 4, 22)
        # 不應 raise
        _assert_attendance_within_retention(date(2021, 4, 21), today=today)

    def test_10_years_ago_can_delete(self):
        today = date(2026, 4, 22)
        _assert_attendance_within_retention(date(2015, 1, 1), today=today)

    def test_leap_year_today_does_not_crash(self):
        """today=2024-02-29 時計算 cutoff 不應 ValueError"""
        today = date(2024, 2, 29)
        # 不應 crash；2019-02-28 仍在保存期內
        with pytest.raises(HTTPException):
            _assert_attendance_within_retention(date(2019, 3, 1), today=today)
