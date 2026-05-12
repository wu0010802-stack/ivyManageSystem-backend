"""Tests for milestone detection pure functions."""

from datetime import date

import pytest

from services.milestone_detector import (
    detect_birthdays,
    detect_first_day,
    detect_graduation,
    detect_perfect_attendance_months,
)


class _Student:
    def __init__(
        self,
        id=1,
        birthday=None,
        enrollment_date=None,
        graduation_date=None,
        lifecycle_status="active",
    ):
        self.id = id
        self.birthday = birthday
        self.enrollment_date = enrollment_date
        self.graduation_date = graduation_date
        self.lifecycle_status = lifecycle_status


def test_detect_first_day():
    s = _Student(enrollment_date=date(2024, 9, 1))
    out = detect_first_day(s)
    assert len(out) == 1
    assert out[0]["milestone_type"] == "first_day"
    assert out[0]["achieved_on"] == date(2024, 9, 1)
    assert out[0]["source_type"] == "auto_enrollment"


def test_detect_first_day_no_enrollment_date():
    s = _Student(enrollment_date=None)
    assert detect_first_day(s) == []


def test_detect_birthdays_within_age_range():
    s = _Student(birthday=date(2022, 3, 5))
    today = date(2026, 5, 1)
    out = detect_birthdays(s, today)
    # 2 歲 (2024/3/5), 3 歲 (2025/3/5), 4 歲 (2026/3/5)
    assert len(out) >= 3
    assert all(o["milestone_type"] == "birthday" for o in out)
    assert all(o["achieved_on"] <= today for o in out)


def test_detect_birthdays_skips_future_birthdays():
    s = _Student(birthday=date(2024, 12, 25))
    today = date(2026, 5, 1)
    out = detect_birthdays(s, today)
    # 1 歲 (2025/12/25) 才到；2 歲 (2026/12/25) 未到
    assert len(out) == 1
    assert out[0]["achieved_on"] == date(2025, 12, 25)


def test_detect_birthdays_no_birthday_returns_empty():
    s = _Student(birthday=None)
    assert detect_birthdays(s, date(2026, 1, 1)) == []


def test_detect_graduation_only_if_graduated():
    s = _Student(graduation_date=date(2025, 7, 31), lifecycle_status="graduated")
    out = detect_graduation(s)
    assert len(out) == 1
    assert out[0]["milestone_type"] == "graduation"
    assert out[0]["achieved_on"] == date(2025, 7, 31)


def test_detect_graduation_active_student_returns_empty():
    s = _Student(lifecycle_status="active")
    assert detect_graduation(s) == []


def test_detect_perfect_attendance_months_basic():
    student_id = 1
    records = [
        {"date": date(2026, 4, 7), "status": "出席"},
        {"date": date(2026, 4, 14), "status": "出席"},
        {"date": date(2026, 4, 21), "status": "出席"},
        {"date": date(2026, 5, 5), "status": "請假"},
        {"date": date(2026, 5, 12), "status": "出席"},
    ]
    out = detect_perfect_attendance_months(
        student_id, records, reference_date=date(2026, 5, 31)
    )
    months = [o["achieved_on"] for o in out]
    assert any(m.year == 2026 and m.month == 4 for m in months)
    assert not any(m.year == 2026 and m.month == 5 for m in months)


def test_detect_perfect_attendance_months_min_3_days():
    """少於 3 筆紀錄不算全勤（避免月初剛開學就觸發）。"""
    student_id = 1
    records = [
        {"date": date(2026, 4, 7), "status": "出席"},
        {"date": date(2026, 4, 14), "status": "出席"},
    ]
    out = detect_perfect_attendance_months(
        student_id, records, reference_date=date(2026, 4, 30)
    )
    assert out == []
