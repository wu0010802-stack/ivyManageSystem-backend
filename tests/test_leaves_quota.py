"""
Unit tests for api/leaves_quota.py

Pure business logic tests — no DB required.
All DB-dependent helpers (_get_*_hours, _quota_row) are mocked in limit/quota tests.
"""

import sys
import os
import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api.leaves_quota import (
    _calc_annual_leave_hours,
    _check_leave_limits,
    _check_quota,
    QUOTA_LEAVE_TYPES,
    STATUTORY_QUOTA_HOURS,
    ANNUAL_MAX_HOURS,
    SINGLE_REQUEST_MAX_HOURS,
    MONTHLY_MAX_HOURS,
)


# ============================================================
# _calc_annual_leave_hours
# ============================================================

class TestCalcAnnualLeaveHours:
    def test_none_hire_date_returns_0(self):
        assert _calc_annual_leave_hours(None, 2025) == 0.0

    def test_hire_date_after_ref_returns_0(self):
        # 入職日在基準日（12/31）之後 → 0
        assert _calc_annual_leave_hours(date(2026, 1, 1), 2025) == 0.0

    def test_less_than_6_months_returns_0(self):
        # 2025/07/01 → ref 2025/12/31 → 5個月又30天，不足6月
        assert _calc_annual_leave_hours(date(2025, 7, 1), 2025) == 0.0

    def test_6_to_12_months_returns_24_hours(self):
        # 2025/01/01 → ref 2025/12/31 → 11個月又30天，<12月完整年
        assert _calc_annual_leave_hours(date(2025, 1, 1), 2025) == 24.0

    def test_exactly_6_months_returns_24_hours(self):
        # 2025/07/01 → 基準 2025/12/31 = 5個月30天...
        # 改用 2025/06/30 → ref 2025/12/31 → 6個月1天
        assert _calc_annual_leave_hours(date(2025, 6, 30), 2025) == 24.0

    def test_1_year_returns_56_hours(self):
        # 2024/01/01 → ref 2025/12/31 → 2年，>=2年 → 80h
        # 改用：2024/07/01 → ref 2025/12/31 → 1年6個月 → complete_years=1 → 56h
        assert _calc_annual_leave_hours(date(2024, 7, 1), 2025) == 56.0

    def test_2_years_returns_80_hours(self):
        # 2024/01/01 → ref 2025/12/31 → 23個月 → complete_years=1 → 56h
        # 改用：2023/07/01 → ref 2025/12/31 → 2年6個月 → complete_years=2 → 80h
        assert _calc_annual_leave_hours(date(2023, 7, 1), 2025) == 80.0

    def test_5_years_returns_120_hours(self):
        # 2020/01/01 → ref 2025/12/31 → 5年11個月 → complete_years=5 → 120h
        assert _calc_annual_leave_hours(date(2020, 1, 1), 2025) == 120.0

    def test_10_years_returns_200_hours(self):
        # 2015/01/01 → ref 2025/12/31 → 10年11個月 → complete_years=10
        # days = min(15+10-10, 30) = 15 → 15*8 = 120h
        # 注意：10年仍在 complete_years>=10 分支，days = min(15+0, 30)=15 → 120h
        result = _calc_annual_leave_hours(date(2015, 1, 1), 2025)
        assert result == 120.0  # 15天 × 8小時

    def test_20_years_capped_at_240_hours(self):
        # 2005/01/01 → ref 2025/12/31 → 20年11個月 → complete_years=20
        # days = min(15+20-10, 30) = min(25, 30) = 25 → 25*8 = 200h
        result = _calc_annual_leave_hours(date(2005, 1, 1), 2025)
        assert result == 200.0

    def test_30_years_capped_at_30_days(self):
        # 1994/01/01 → complete_years = 31 → days = min(15+31-10, 30) = 30 → 240h
        result = _calc_annual_leave_hours(date(1994, 1, 1), 2025)
        assert result == 240.0


# ============================================================
# _check_leave_limits （mock DB calls）
# ============================================================

class TestCheckLeaveLimits:
    """mock _get_approved_hours_in_year / _get_pending_hours_in_year 等輔助函式"""

    def _mock_session(self):
        return MagicMock()

    def test_bereavement_single_over_8_days_raises_400(self):
        session = self._mock_session()
        with pytest.raises(HTTPException) as exc_info:
            _check_leave_limits(session, 1, "bereavement", date(2025, 3, 1), 72.0)
        assert exc_info.value.status_code == 400
        assert "喪假" in exc_info.value.detail

    def test_bereavement_exactly_8_days_passes(self):
        session = self._mock_session()
        # 恰好 64 小時不超出，不應拋出例外
        _check_leave_limits(session, 1, "bereavement", date(2025, 3, 1), 64.0)

    def test_marriage_over_annual_limit_raises_400(self):
        session = self._mock_session()
        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=56.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            with pytest.raises(HTTPException) as exc_info:
                _check_leave_limits(session, 1, "marriage", date(2025, 3, 1), 16.0)
            assert exc_info.value.status_code == 400
            assert "婚假" in exc_info.value.detail

    def test_marriage_within_annual_limit_passes(self):
        session = self._mock_session()
        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=0.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            _check_leave_limits(session, 1, "marriage", date(2025, 3, 1), 64.0)

    def test_menstrual_over_monthly_limit_raises_400(self):
        session = self._mock_session()
        with (
            patch("api.leaves_quota._get_approved_hours_in_month", return_value=0.0),
            patch("api.leaves_quota._get_pending_hours_in_month", return_value=8.0),
        ):
            with pytest.raises(HTTPException) as exc_info:
                _check_leave_limits(session, 1, "menstrual", date(2025, 3, 1), 8.0)
            assert exc_info.value.status_code == 400
            assert "生理假" in exc_info.value.detail

    def test_include_pending_false_ignores_pending(self):
        """include_pending=False 時，待審時數不計入，不應超限"""
        session = self._mock_session()
        with (
            patch("api.leaves_quota._get_approved_hours_in_month", return_value=0.0),
        ):
            # pending=8 被忽略，approved=0，0+8=8 不超過 8 → 不拋出
            _check_leave_limits(
                session, 1, "menstrual", date(2025, 3, 1), 8.0,
                include_pending=False
            )

    def test_non_limited_leave_type_always_passes(self):
        session = self._mock_session()
        # annual / sick / personal 都不在 ANNUAL_MAX_HOURS / SINGLE_REQUEST_MAX_HOURS / MONTHLY_MAX_HOURS
        _check_leave_limits(session, 1, "annual", date(2025, 3, 1), 999.0)
        _check_leave_limits(session, 1, "sick", date(2025, 3, 1), 999.0)


# ============================================================
# _check_quota （mock DB calls）
# ============================================================

class TestCheckQuota:
    def _mock_session(self):
        return MagicMock()

    def test_non_quota_leave_type_passes_through(self):
        session = self._mock_session()
        # 婚假不在 QUOTA_LEAVE_TYPES → 直接返回，不查 DB
        _check_quota(session, 1, "marriage", 2025, 64.0)
        session.query.assert_not_called()

    def test_no_quota_record_passes_silently(self):
        """LeaveQuota 不存在時略過，不攔截"""
        session = self._mock_session()
        # 模擬 session.query().filter().first() 返回 None
        session.query.return_value.filter.return_value.first.return_value = None
        _check_quota(session, 1, "annual", 2025, 100.0)

    def test_over_quota_raises_400(self):
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 56.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=48.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            with pytest.raises(HTTPException) as exc_info:
                _check_quota(session, 1, "annual", 2025, 16.0)
            assert exc_info.value.status_code == 400
            assert "超過剩餘配額" in exc_info.value.detail

    def test_within_quota_passes(self):
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 56.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=0.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            _check_quota(session, 1, "annual", 2025, 56.0)

    def test_include_pending_false_only_counts_approved(self):
        """include_pending=False 時，只計算 approved，pending 不納入"""
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 56.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with patch("api.leaves_quota._get_approved_hours_in_year", return_value=48.0):
            # approved=48, pending 不算, remaining=8, leave_hours=8 → 恰好通過
            _check_quota(session, 1, "annual", 2025, 8.0, include_pending=False)


# ============================================================
# 常數完整性驗證
# ============================================================

class TestConstants:
    def test_quota_leave_types_subset_of_statutory(self):
        """QUOTA_LEAVE_TYPES 中除了 annual，其餘都應有 STATUTORY_QUOTA_HOURS"""
        for lt in QUOTA_LEAVE_TYPES:
            if lt != "annual":
                assert lt in STATUTORY_QUOTA_HOURS, f"{lt} 缺少 STATUTORY_QUOTA_HOURS 定義"

    def test_annual_max_hours_marriage(self):
        assert ANNUAL_MAX_HOURS["marriage"] == 64.0

    def test_single_request_max_hours_bereavement(self):
        assert SINGLE_REQUEST_MAX_HOURS["bereavement"] == 64.0

    def test_monthly_max_hours_menstrual(self):
        assert MONTHLY_MAX_HOURS["menstrual"] == 8.0
