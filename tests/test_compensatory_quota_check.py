"""Bug 回歸:補休假單必須檢查補休配額。

Bug 描述:
    `compensatory` 不在 QUOTA_LEAVE_TYPES,導致 _check_quota 直接 return,
    補休假單建立/核准時不檢查配額。員工可申請超過累積配額的補休,
    或在從未累積過任何補休配額的情況下也能送出補休假單。

修復方向(路 B):
    在 api/leaves.py:_guard_leave_quota 增加 compensatory 分支,
    呼叫新建的 api.leaves_quota._check_compensatory_quota helper。
    helper 行為:
      - LeaveQuota 不存在 → 視為 total_hours=0,有任何申請小時都拋 400
      - 行為類似 _check_quota,但對「配額不存在」採嚴格策略
"""

import sys
import os
import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.leaves_quota import _check_compensatory_quota
from api.leaves import _guard_leave_quota


class TestCheckCompensatoryQuota:
    """補休配額專用 helper:配額不存在 = 0 小時。"""

    def _mock_session(self):
        return MagicMock()

    def test_no_quota_record_blocks_any_request(self):
        """LeaveQuota 不存在 → 任何申請小時都拋 400(視同 total=0)"""
        session = self._mock_session()
        session.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(HTTPException) as exc:
            _check_compensatory_quota(session, 1, 2026, 1.0)
        assert exc.value.status_code == 400
        assert "補休" in exc.value.detail

    def test_within_quota_passes(self):
        """配額 8h,已用 0,申請 4h → 通過"""
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 8.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=0.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            _check_compensatory_quota(session, 1, 2026, 4.0)

    def test_over_quota_raises(self):
        """配額 8h,已用 8h,申請 1h → 400"""
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 8.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=8.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
        ):
            with pytest.raises(HTTPException) as exc:
                _check_compensatory_quota(session, 1, 2026, 1.0)
            assert exc.value.status_code == 400

    def test_pending_counted_when_include_pending_true(self):
        """配額 16h,已核准 8h,待審 4h → 剩 4h,申請 8h 應 400"""
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 16.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with (
            patch("api.leaves_quota._get_approved_hours_in_year", return_value=8.0),
            patch("api.leaves_quota._get_pending_hours_in_year", return_value=4.0),
        ):
            with pytest.raises(HTTPException) as exc:
                _check_compensatory_quota(
                    session, 1, 2026, 8.0, include_pending=True
                )
            assert exc.value.status_code == 400

    def test_pending_ignored_when_include_pending_false(self):
        """include_pending=False:配額 16h,已核准 8h,待審 4h(被忽略),申請 8h → 通過"""
        session = self._mock_session()
        mock_quota = MagicMock()
        mock_quota.total_hours = 16.0
        session.query.return_value.filter.return_value.first.return_value = mock_quota

        with patch(
            "api.leaves_quota._get_approved_hours_in_year", return_value=8.0
        ):
            _check_compensatory_quota(
                session, 1, 2026, 8.0, include_pending=False
            )


class TestGuardLeaveQuotaCompensatoryDispatch:
    """_guard_leave_quota 對 compensatory 必須走補休配額分支(不可走 _check_quota 的 略過 None)。"""

    def test_compensatory_dispatched_to_compensatory_helper(self):
        """compensatory 必須呼叫 _check_compensatory_quota,不可走 _check_quota 而被略過"""
        session = MagicMock()
        with (
            patch(
                "api.leaves._check_compensatory_quota"
            ) as mock_comp,
            patch("api.leaves._check_quota") as mock_quota,
        ):
            _guard_leave_quota(
                session,
                employee_id=1,
                leave_type="compensatory",
                year=2026,
                leave_hours=4.0,
                is_hospitalized=False,
            )
            mock_comp.assert_called_once()
            mock_quota.assert_not_called()
