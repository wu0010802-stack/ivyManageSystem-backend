"""analytics constants & helper tests"""

from datetime import date

import pytest

from services.analytics.constants import (
    CHURN_CONSECUTIVE_ABSENCE_DAYS,
    CHURN_ON_LEAVE_DAYS,
    CHURN_FEE_OVERDUE_DAYS,
    FUNNEL_STAGES,
    RETENTION_WINDOWS_DAYS,
    parse_roc_month,
    term_start_date,
)


def test_thresholds_match_spec():
    assert CHURN_CONSECUTIVE_ABSENCE_DAYS == 3
    assert CHURN_ON_LEAVE_DAYS == 30
    assert CHURN_FEE_OVERDUE_DAYS == 14


def test_funnel_stages_order_and_keys():
    assert FUNNEL_STAGES == [
        "lead",
        "deposit",
        "enrolled",
        "active",
        "retained_1m",
        "retained_6m",
    ]


def test_retention_windows():
    assert RETENTION_WINDOWS_DAYS == {"1m": 30, "6m": 180}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("115.03", (2026, 3)),
        ("114.12", (2025, 12)),
        ("100.01", (2011, 1)),
    ],
)
def test_parse_roc_month_valid(raw, expected):
    assert parse_roc_month(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "115", "115/03", "115.13", "115.00", None])
def test_parse_roc_month_invalid(raw):
    assert parse_roc_month(raw) is None


def test_term_start_date_first_term():
    # period 字串 "2025-1" → 上學期 9/1
    assert term_start_date("2025-1") == date(2025, 9, 1)


def test_term_start_date_second_term():
    # period 字串 "2025-2" → 下學期 隔年 2/1
    assert term_start_date("2025-2") == date(2026, 2, 1)


@pytest.mark.parametrize("raw", ["", "abc", "2025", "2025-3", "2025-0", None])
def test_term_start_date_invalid(raw):
    assert term_start_date(raw) is None
