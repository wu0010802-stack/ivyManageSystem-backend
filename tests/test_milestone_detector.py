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


# 全勤月 = 已結束月份中，每個官方工作日都「出席」（遲到/缺席/請假皆破功）。
# official_workdays 由 caller 用 workday_rules 算好傳入（detector 維持純函式）。


def _present(dates):
    return [{"date": d, "status": "出席"} for d in dates]


def test_perfect_attendance_awarded_when_all_official_workdays_present():
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    out = detect_perfect_attendance_months(
        1,
        _present(workdays),
        reference_date=date(2026, 5, 1),
        official_workdays=workdays,
    )
    assert [o["achieved_on"] for o in out] == [date(2026, 4, 1)]
    assert out[0]["milestone_type"] == "perfect_attendance_month"
    assert out[0]["source_ref_id"] == 202604


def test_no_badge_when_a_workday_record_missing():
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    records = _present({date(2026, 4, 1), date(2026, 4, 2)})  # 缺 4/3
    out = detect_perfect_attendance_months(
        1, records, reference_date=date(2026, 5, 1), official_workdays=workdays
    )
    assert out == []


def test_late_arrival_breaks_perfect_attendance():
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    records = _present({date(2026, 4, 1), date(2026, 4, 2)}) + [
        {"date": date(2026, 4, 3), "status": "遲到"}
    ]
    out = detect_perfect_attendance_months(
        1, records, reference_date=date(2026, 5, 1), official_workdays=workdays
    )
    assert out == []


def test_absence_breaks_perfect_attendance():
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    records = _present({date(2026, 4, 1), date(2026, 4, 2)}) + [
        {"date": date(2026, 4, 3), "status": "缺席"}
    ]
    out = detect_perfect_attendance_months(
        1, records, reference_date=date(2026, 5, 1), official_workdays=workdays
    )
    assert out == []


def test_sparse_month_no_longer_false_positive():
    """原 bug：該月 20 個工作日只記 3 天出席就拿章。修後不發。"""
    workdays = {date(2026, 4, d) for d in range(1, 21)}
    records = _present({date(2026, 4, 1), date(2026, 4, 8), date(2026, 4, 15)})
    out = detect_perfect_attendance_months(
        1, records, reference_date=date(2026, 5, 1), official_workdays=workdays
    )
    assert out == []


def test_in_progress_month_not_awarded():
    """未結束的當月不發章，即使目前每個工作日都出席。"""
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    out = detect_perfect_attendance_months(
        1,
        _present(workdays),
        reference_date=date(2026, 4, 15),
        official_workdays=workdays,
    )
    assert out == []


def test_weekends_and_holidays_ignored():
    """非工作日（週末/假日）無記錄不影響全勤判定。"""
    workdays = {date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)}
    out = detect_perfect_attendance_months(
        1,
        _present(workdays),
        reference_date=date(2026, 5, 1),
        official_workdays=workdays,
    )
    assert [o["achieved_on"] for o in out] == [date(2026, 4, 1)]


def test_no_official_workdays_no_badge():
    """該期間無官方工作日（空集合）不發章。"""
    out = detect_perfect_attendance_months(
        1, [], reference_date=date(2026, 5, 1), official_workdays=set()
    )
    assert out == []
