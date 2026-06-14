from datetime import date

from scripts.seedgen.calendar import (
    month_status,
    all_months,
    closed_months,
    current_month,
    workdays,
)


def test_status_partition():
    cfg_today = date(2026, 2, 16)
    ys = date(2025, 8, 1)
    ye = date(2026, 7, 31)
    assert month_status(2025, 8, cfg_today) == "closed"
    assert month_status(2026, 1, cfg_today) == "closed"
    assert month_status(2026, 2, cfg_today) == "in_progress"
    assert month_status(2026, 3, cfg_today) == "future"


def test_closed_months_count():
    months = closed_months(date(2025, 8, 1), date(2026, 2, 16))
    assert (2025, 8) in months and (2026, 1) in months and (2026, 2) not in months
    assert len(months) == 6


def test_workdays_excludes_weekends():
    wd = workdays(2025, 9, upto=None)
    assert all(d.weekday() < 5 for d in wd)
