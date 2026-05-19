"""recurrence.expand_event 純函式測試。

純函式無 DB 依賴；只測「給定 event_date/end_date/rule/window，
回傳 (start, end) tuple list」的純邏輯。
"""

from datetime import date

import pytest

from utils.recurrence import expand_event

# ----- 無 rule（向後相容）-----


def test_recurrence_null_returns_single_occurrence():
    result = expand_event(
        event_date=date(2026, 5, 20),
        end_date=None,
        rule=None,
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert result == [(date(2026, 5, 20), date(2026, 5, 20))]


def test_recurrence_null_outside_window_returns_empty():
    result = expand_event(
        event_date=date(2026, 1, 5),
        end_date=None,
        rule=None,
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert result == []


# ----- weekly -----


def test_weekly_expand_4_weeks():
    result = expand_event(
        event_date=date(2026, 5, 5),  # 週二（weekday=1）
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    starts = [s for s, _ in result]
    assert starts == [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
    ]


def test_weekly_until_inclusive():
    result = expand_event(
        event_date=date(2026, 5, 5),
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-12"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert [s for s, _ in result] == [date(2026, 5, 5), date(2026, 5, 12)]


def test_window_clipping():
    result = expand_event(
        event_date=date(2026, 1, 6),  # 週二
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-12-29"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    starts = [s for s, _ in result]
    assert len(starts) == 4
    assert min(starts) >= date(2026, 5, 1)
    assert max(starts) <= date(2026, 5, 31)


# ----- monthly_day -----


def test_monthly_day_15_full_year():
    result = expand_event(
        event_date=date(2026, 1, 15),
        end_date=None,
        rule={"type": "monthly_day", "day": 15, "until": "2026-12-15"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
    )
    assert len(result) == 12
    assert [s.month for s, _ in result] == list(range(1, 13))


def test_monthly_day_31_skips_short_months():
    result = expand_event(
        event_date=date(2026, 1, 31),
        end_date=None,
        rule={"type": "monthly_day", "day": 31, "until": "2026-12-31"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
    )
    months = [s.month for s, _ in result]
    assert months == [1, 3, 5, 7, 8, 10, 12]


# ----- monthly_nth -----


def test_monthly_nth_first_monday():
    result = expand_event(
        event_date=date(2026, 5, 4),  # 5 月第一個週一（weekday=0）
        end_date=None,
        rule={"type": "monthly_nth", "nth": 1, "weekday": 0, "until": "2026-10-31"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 10, 31),
    )
    starts = [s for s, _ in result]
    assert starts == [
        date(2026, 5, 4),
        date(2026, 6, 1),
        date(2026, 7, 6),
        date(2026, 8, 3),
        date(2026, 9, 7),
        date(2026, 10, 5),
    ]


def test_monthly_nth_last_friday():
    result = expand_event(
        event_date=date(2026, 5, 29),  # 5 月最後一個週五（weekday=4）
        end_date=None,
        rule={"type": "monthly_nth", "nth": -1, "weekday": 4, "until": "2026-07-31"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 7, 31),
    )
    starts = [s for s, _ in result]
    assert starts == [date(2026, 5, 29), date(2026, 6, 26), date(2026, 7, 31)]


def test_monthly_nth_fifth_skipped_when_absent():
    result = expand_event(
        event_date=date(2026, 1, 29),  # 2026-01 第 5 個週四（weekday=3）— 存在
        end_date=None,
        rule={"type": "monthly_nth", "nth": 5, "weekday": 3, "until": "2026-04-30"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 4, 30),
    )
    starts = [s for s, _ in result]
    # 2026-01-29 ✓ 2026-04-30 ✓；2/3 月不存在第 5 週四
    assert starts == [date(2026, 1, 29), date(2026, 4, 30)]


# ----- multi-day recurring -----


def test_multi_day_recurring():
    result = expand_event(
        event_date=date(2026, 5, 5),
        end_date=date(2026, 5, 6),
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-19"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert result == [
        (date(2026, 5, 5), date(2026, 5, 6)),
        (date(2026, 5, 12), date(2026, 5, 13)),
        (date(2026, 5, 19), date(2026, 5, 20)),
    ]


# ----- validate_rule -----

from utils.recurrence import validate_rule


def test_validate_weekday_mismatch_rejected():
    """rule weekday=1（週二）但 event_date 是週三 → ValueError。"""
    with pytest.raises(ValueError, match="weekday"):
        validate_rule(
            event_date=date(2026, 5, 6),  # 週三
            rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        )


def test_validate_until_over_730_days_rejected():
    """until - event_date > 730 → ValueError runaway。"""
    with pytest.raises(ValueError, match="730"):
        validate_rule(
            event_date=date(2026, 1, 1),
            rule={"type": "weekly", "weekday": 3, "until": "2029-01-01"},  # ~1096 days
        )


def test_validate_unknown_type_rejected():
    """unknown rule type → ValueError。"""
    with pytest.raises(ValueError, match="rule type"):
        validate_rule(
            event_date=date(2026, 5, 5),
            rule={"type": "yearly", "until": "2030-01-01"},
        )


def test_validate_monthly_day_31_for_jan_31_event_passes():
    """event_date 是 1/31 + monthly_day 31 → 通過。"""
    validate_rule(
        event_date=date(2026, 1, 31),
        rule={"type": "monthly_day", "day": 31, "until": "2026-06-30"},
    )  # 不 raise
