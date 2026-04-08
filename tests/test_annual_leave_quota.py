"""
純邏輯單元測試：_calculate_annual_leave_quota（勞基法特休週年制）
"""
import calendar
import pytest
from datetime import date, timedelta

from api.portal._shared import _calculate_annual_leave_quota


def _hire_date_months_ago(months: int) -> date:
    """回傳距今 months 個月前的日期（保持同一天，確保精確觸發分界）。
    若目標月份不足該天（如 3/31 - 6月 = 9/31 不存在），取當月最後一天。
    """
    today = date.today()
    y = today.year - months // 12
    m = today.month - months % 12
    if m <= 0:
        m += 12
        y -= 1
    # 夾限到目標月份的最後一天（處理如 3/31 → 9/30 的情況）
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(today.day, last_day))


def _hire_date_years_ago(years: int) -> date:
    return _hire_date_months_ago(years * 12)


class TestCalculateAnnualLeaveQuota:
    def test_none_hire_date_returns_zero(self):
        assert _calculate_annual_leave_quota(None) == 0

    def test_less_than_6_months_returns_zero(self):
        """未滿 6 個月，特休 0 天"""
        hire = _hire_date_months_ago(3)
        assert _calculate_annual_leave_quota(hire) == 0

    def test_exactly_6_months_returns_3(self):
        """滿 6 個月未滿 1 年，特休 3 天"""
        hire = _hire_date_months_ago(6)
        assert _calculate_annual_leave_quota(hire) == 3

    def test_11_months_returns_3(self):
        """滿 11 個月，仍屬 6–12 個月區間"""
        hire = _hire_date_months_ago(11)
        assert _calculate_annual_leave_quota(hire) == 3

    def test_1_year_returns_7(self):
        """滿 1 年，特休 7 天"""
        hire = _hire_date_years_ago(1)
        assert _calculate_annual_leave_quota(hire) == 7

    def test_2_years_returns_10(self):
        hire = _hire_date_years_ago(2)
        assert _calculate_annual_leave_quota(hire) == 10

    def test_3_years_returns_14(self):
        hire = _hire_date_years_ago(3)
        assert _calculate_annual_leave_quota(hire) == 14

    def test_4_years_returns_14(self):
        """3–5 年區間，仍為 14 天"""
        hire = _hire_date_years_ago(4)
        assert _calculate_annual_leave_quota(hire) == 14

    def test_5_years_returns_15(self):
        hire = _hire_date_years_ago(5)
        assert _calculate_annual_leave_quota(hire) == 15

    def test_9_years_returns_15(self):
        """5–10 年區間，仍為 15 天"""
        hire = _hire_date_years_ago(9)
        assert _calculate_annual_leave_quota(hire) == 15

    def test_10_years_returns_15(self):
        """滿 10 年：15天（extra_days = 0，第 11 年起才 +1）"""
        hire = _hire_date_years_ago(10)
        assert _calculate_annual_leave_quota(hire) == 15

    def test_11_years_returns_16(self):
        """滿 11 年：15 + 1 = 16 天"""
        hire = _hire_date_years_ago(11)
        assert _calculate_annual_leave_quota(hire) == 16

    def test_20_years_returns_25(self):
        """滿 20 年：15 + 10 = 25 天"""
        hire = _hire_date_years_ago(20)
        assert _calculate_annual_leave_quota(hire) == 25

    def test_cap_at_30_days(self):
        """滿 25 年達上限 30 天（15 + 15 = 30）"""
        hire = _hire_date_years_ago(25)
        assert _calculate_annual_leave_quota(hire) == 30

    def test_very_long_tenure_capped_at_30(self):
        hire = _hire_date_years_ago(40)
        assert _calculate_annual_leave_quota(hire) == 30
