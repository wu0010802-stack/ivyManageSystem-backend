import os, sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.attendance_shift_window import (
    resolve_shift_window,
    compute_status_for_employee_date,
)


class _Emp:
    def __init__(self, id, work_start_time=None, work_end_time=None):
        self.id = id
        self.work_start_time = work_start_time
        self.work_end_time = work_end_time


def test_daily_shift_takes_priority():
    emp = _Emp(1, work_start_time="08:00", work_end_time="17:00")
    daily = {(1, date(2026, 2, 5)): {"work_start": "13:00", "work_end": "22:00"}}
    start_dt, end_dt = resolve_shift_window(emp, date(2026, 2, 5), daily, {})
    assert start_dt == datetime(2026, 2, 5, 13, 0)
    assert end_dt == datetime(2026, 2, 5, 22, 0)


def test_falls_back_to_employee_work_times():
    emp = _Emp(1, work_start_time="09:30", work_end_time="18:30")
    start_dt, end_dt = resolve_shift_window(emp, date(2026, 2, 5), {}, {})
    assert start_dt == datetime(2026, 2, 5, 9, 30)
    assert end_dt == datetime(2026, 2, 5, 18, 30)


def test_default_when_unset():
    emp = _Emp(1)
    start_dt, end_dt = resolve_shift_window(emp, date(2026, 2, 5), {}, {})
    assert (start_dt.hour, end_dt.hour) == (8, 17)


def test_overnight_end_rolls_to_next_day():
    emp = _Emp(1, work_start_time="16:00", work_end_time="01:00")
    start_dt, end_dt = resolve_shift_window(emp, date(2026, 2, 5), {}, {})
    assert start_dt == datetime(2026, 2, 5, 16, 0)
    assert end_dt == datetime(2026, 2, 6, 1, 0)


def test_weekly_assignment_only_for_head_or_assistant():
    emp = _Emp(7, work_start_time="08:00", work_end_time="17:00")
    week_start = date(2026, 2, 2)
    sched = {(7, week_start): {"work_start": "13:00", "work_end": "22:00"}}
    s1, e1 = resolve_shift_window(
        emp, date(2026, 2, 5), {}, sched, is_head_teacher=False, is_assistant=False
    )
    assert s1.hour == 8
    s2, e2 = resolve_shift_window(
        emp, date(2026, 2, 5), {}, sched, is_head_teacher=True
    )
    assert (s2.hour, e2.hour) == (13, 22)


def test_compute_status_uses_window():
    emp = _Emp(1)
    daily = {(1, date(2026, 2, 5)): {"work_start": "13:00", "work_end": "22:00"}}
    is_late, late_min, is_early, early_min, status = compute_status_for_employee_date(
        emp,
        date(2026, 2, 5),
        datetime(2026, 2, 5, 13, 2),
        datetime(2026, 2, 5, 22, 0),
        daily,
        {},
    )
    assert is_late is True and late_min == 2
