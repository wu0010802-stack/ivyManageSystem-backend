"""eval framework 揭露的 proration 輸入驗證漏洞回歸測試。

3 條真 bug:
1. `if not contracted_base` 對 -30000 為 truthy → 算出負薪 -16451
2. `_prorate_for_period` 月份越界丟 IllegalMonthError 而非 ValueError
   (與 `_build_expected_workdays` 行為不一致)
3. `_prorate_for_period` 同月 resign<hire 算出負薪
"""

from datetime import date

import pytest

from services.salary.proration import (
    _prorate_base_salary,
    _prorate_for_period,
)


class TestNegativeContractedBase:
    """負契約底薪應 reject(原 `if not contracted_base` 對 -30000 truthy 通過)"""

    def test_prorate_base_negative_raises(self):
        with pytest.raises(
            ValueError, match="contracted_base|底薪.*不可為負|必須.*非負"
        ):
            _prorate_base_salary(-30000, date(2026, 5, 15), 2026, 5)

    def test_prorate_period_negative_raises(self):
        with pytest.raises(
            ValueError, match="contracted_base|底薪.*不可為負|必須.*非負"
        ):
            _prorate_for_period(-30000, date(2026, 1, 1), None, 2026, 5)

    def test_prorate_base_zero_returns_zero(self):
        """contracted_base=0 仍維持回 0(原行為)"""
        assert _prorate_base_salary(0, date(2026, 5, 15), 2026, 5) == 0


class TestInvalidMonthGuard:
    """month 越界應 raise ValueError(統一 _build_expected_workdays 既有行為)"""

    def test_prorate_base_month_zero_raises(self):
        with pytest.raises(ValueError, match="month"):
            _prorate_base_salary(30000, date(2026, 5, 15), 2026, 0)

    def test_prorate_base_month_thirteen_raises(self):
        with pytest.raises(ValueError, match="month"):
            _prorate_base_salary(30000, date(2026, 5, 15), 2026, 13)

    def test_prorate_period_month_zero_raises(self):
        with pytest.raises(ValueError, match="month"):
            _prorate_for_period(30000, None, None, 2026, 0)

    def test_prorate_period_month_negative_raises(self):
        with pytest.raises(ValueError, match="month"):
            _prorate_for_period(30000, None, None, 2026, -1)


class TestResignBeforeHireGuard:
    """同月內 resign 早於 hire 視為資料異常,應 raise(防止算出負薪)"""

    def test_resign_before_hire_same_month_raises(self):
        with pytest.raises(ValueError, match="resign|離職.*早於|hire"):
            _prorate_for_period(
                30000,
                date(2026, 5, 20),
                date(2026, 5, 10),
                2026,
                5,
            )

    def test_resign_equal_hire_allowed(self):
        """同日入職離職允許(worked_days=1,薪資 = 1/31)"""
        result = _prorate_for_period(
            30000,
            date(2026, 5, 15),
            date(2026, 5, 15),
            2026,
            5,
        )
        assert result == pytest.approx(30000 / 31)

    def test_resign_after_hire_normal_flow(self):
        """正常順序:同月入離職 hire=5 resign=20"""
        result = _prorate_for_period(
            30000,
            date(2026, 5, 5),
            date(2026, 5, 20),
            2026,
            5,
        )
        # worked_days = 20 - 5 + 1 = 16
        assert result == pytest.approx(30000 * 16 / 31)
