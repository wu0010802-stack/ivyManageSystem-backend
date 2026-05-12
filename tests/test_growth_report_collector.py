"""Tests for growth_report_collector pure functions."""

from __future__ import annotations

from datetime import date


def test_summarize_attendance_returns_counts_and_rate():
    from services.growth_report_collector import summarize_attendance

    records = [
        {"date": date(2026, 4, 1), "status": "出席"},
        {"date": date(2026, 4, 2), "status": "出席"},
        {"date": date(2026, 4, 3), "status": "請假"},
        {"date": date(2026, 4, 4), "status": "病假"},
        {"date": date(2026, 4, 5), "status": "出席"},
    ]
    summary = summarize_attendance(records)
    assert summary["total_days"] == 5
    assert summary["present_days"] == 3
    assert summary["leave_days"] == 1
    assert summary["sick_days"] == 1
    assert abs(summary["present_rate"] - 0.6) < 0.001


def test_summarize_attendance_empty_no_division_by_zero():
    from services.growth_report_collector import summarize_attendance

    summary = summarize_attendance([])
    assert summary["total_days"] == 0
    assert summary["present_rate"] == 0.0


def test_pick_highlight_observations_prefers_is_highlight():
    from services.growth_report_collector import pick_highlight_observations

    class _O:
        def __init__(self, id, is_highlight, observation_date):
            self.id = id
            self.is_highlight = is_highlight
            self.observation_date = observation_date
            self.narrative = f"observation {id}"
            self.domain = "認知"

    obs = [
        _O(1, False, date(2026, 4, 1)),
        _O(2, True, date(2026, 4, 2)),
        _O(3, True, date(2026, 4, 3)),
        _O(4, False, date(2026, 4, 4)),
        _O(5, False, date(2026, 4, 5)),
    ]
    picked = pick_highlight_observations(obs, max_count=5)
    ids = [o["id"] for o in picked]
    assert ids[0] == 3  # 最新 highlight
    assert ids[1] == 2  # 第二新 highlight
    assert ids[2:] == [5, 4, 1]  # 剩 3 個位置用 non-highlight，依日期 desc


def test_pick_highlight_observations_caps_at_max_count():
    from services.growth_report_collector import pick_highlight_observations

    class _O:
        def __init__(self, id, observation_date):
            self.id = id
            self.is_highlight = True
            self.observation_date = observation_date
            self.narrative = f"o{id}"
            self.domain = None

    obs = [_O(i, date(2026, 4, i)) for i in range(1, 11)]
    picked = pick_highlight_observations(obs, max_count=3)
    assert len(picked) == 3


def test_measurements_to_series_returns_sorted_tuples():
    from services.growth_report_collector import measurements_to_series

    class _M:
        def __init__(self, measured_on, h, w):
            self.measured_on = measured_on
            self.height_cm = h
            self.weight_kg = w

    rows = [
        _M(date(2026, 5, 1), 110.5, 18.5),
        _M(date(2026, 2, 1), 109.0, 18.0),
        _M(date(2026, 4, 1), 110.0, 18.3),
    ]
    series = measurements_to_series(rows)
    # Should be asc by date
    assert series["height"][0][0] == "2026-02-01"
    assert series["height"][-1][0] == "2026-05-01"
    assert series["weight"][0][1] == 18.0
