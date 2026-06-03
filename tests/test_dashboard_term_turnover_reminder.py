"""dashboard 學期切換 reminder：term start_date 起 7 天內顯示。"""

from datetime import date
from unittest.mock import patch

from services.dashboard_query_service import build_term_turnover_reminder


def test_reminder_present_on_start_day():
    with patch(
        "services.dashboard_query_service.today_taipei", return_value=date(2026, 2, 1)
    ):
        r = build_term_turnover_reminder()
    assert r is not None
    assert r["type"] == "academic_term_turnover"
    assert "下學期" in r["title"]


def test_reminder_present_within_7_days():
    with patch(
        "services.dashboard_query_service.today_taipei", return_value=date(2026, 2, 7)
    ):
        assert build_term_turnover_reminder() is not None


def test_reminder_absent_after_7_days():
    with patch(
        "services.dashboard_query_service.today_taipei", return_value=date(2026, 2, 9)
    ):
        assert build_term_turnover_reminder() is None
