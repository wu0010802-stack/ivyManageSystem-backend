"""Bug 回歸:Portal 加班建立必須補上月上限與國定假日類型檢查。

Bug 描述(P1-2):
    api/portal/overtimes.py:create_my_overtime 只檢查時間重疊,沒呼叫:
      - _check_monthly_overtime_cap(46h/月,勞基法第 32 條)
      - _check_overtime_type_calendar(國定假日須用 holiday,勞基法第 37 條)
    管理端 create_overtime 兩道都有,造成教師可從 portal 繞過上限,
    或在國定假日用 weekday/weekend 短付加班費。

修復方向:
    在 portal create_my_overtime 重疊檢查後補上這兩道呼叫。
"""

import sys
import os
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.portal._shared import OvertimeCreatePortal


def _build_payload(overtime_date=date(2026, 3, 20), overtime_type="weekday", hours=4.0):
    return OvertimeCreatePortal(
        overtime_date=overtime_date,
        overtime_type=overtime_type,
        start_time="18:00",
        end_time="22:00",
        hours=hours,
        reason="加班",
        use_comp_leave=False,
    )


def _patched_session_ctx():
    """提供一個假 session,並讓 commit/close 不爆炸。"""
    session = MagicMock()
    return session


def _patched_emp():
    emp = MagicMock()
    emp.id = 1
    emp.name = "測試員工"
    emp.base_salary = 30000
    return emp


class TestPortalCreateOvertimeGuards:
    """重疊檢查後必須呼叫月上限與類型檢查 helper。"""

    def test_monthly_cap_is_invoked(self):
        """portal create_my_overtime 必須呼叫 _check_monthly_overtime_cap"""
        from api.portal import overtimes as portal_ot

        session = _patched_session_ctx()
        emp = _patched_emp()
        data = _build_payload()
        current_user = {"username": "teacher1", "employee_id": 1}

        with (
            patch.object(portal_ot, "get_session", return_value=session),
            patch.object(portal_ot, "_get_employee", return_value=emp),
            patch(
                "api.overtimes._check_overtime_overlap", return_value=None
            ),
            patch(
                "api.overtimes.calculate_overtime_pay", return_value=400.0
            ),
            patch(
                "api.overtimes._check_monthly_overtime_cap"
            ) as mock_monthly,
            patch(
                "api.overtimes._check_overtime_type_calendar"
            ),
        ):
            portal_ot.create_my_overtime(data=data, current_user=current_user)

        assert mock_monthly.called, "月上限檢查未被呼叫"
        # 第一個位置參數應是 session,第二個是 emp.id,第三個是 overtime_date
        args, kwargs = mock_monthly.call_args
        # 容許 kwargs/positional 兩種寫法
        passed = list(args) + list(kwargs.values())
        assert emp.id in passed
        assert data.overtime_date in passed

    def test_calendar_type_is_invoked(self):
        """portal create_my_overtime 必須呼叫 _check_overtime_type_calendar"""
        from api.portal import overtimes as portal_ot

        session = _patched_session_ctx()
        emp = _patched_emp()
        data = _build_payload()
        current_user = {"username": "teacher1", "employee_id": 1}

        with (
            patch.object(portal_ot, "get_session", return_value=session),
            patch.object(portal_ot, "_get_employee", return_value=emp),
            patch(
                "api.overtimes._check_overtime_overlap", return_value=None
            ),
            patch(
                "api.overtimes.calculate_overtime_pay", return_value=400.0
            ),
            patch(
                "api.overtimes._check_monthly_overtime_cap"
            ),
            patch(
                "api.overtimes._check_overtime_type_calendar"
            ) as mock_cal,
        ):
            portal_ot.create_my_overtime(data=data, current_user=current_user)

        assert mock_cal.called, "國定假日類型檢查未被呼叫"

    def test_monthly_cap_violation_propagates_400(self):
        """月上限超出時,portal 必須將 400 透傳給呼叫者(目前會 swallow → 漏網)"""
        from api.portal import overtimes as portal_ot

        session = _patched_session_ctx()
        emp = _patched_emp()
        data = _build_payload()
        current_user = {"username": "teacher1", "employee_id": 1}

        with (
            patch.object(portal_ot, "get_session", return_value=session),
            patch.object(portal_ot, "_get_employee", return_value=emp),
            patch(
                "api.overtimes._check_overtime_overlap", return_value=None
            ),
            patch(
                "api.overtimes.calculate_overtime_pay", return_value=400.0
            ),
            patch(
                "api.overtimes._check_monthly_overtime_cap",
                side_effect=HTTPException(status_code=400, detail="超過 46h 月上限"),
            ),
            patch(
                "api.overtimes._check_overtime_type_calendar"
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                portal_ot.create_my_overtime(data=data, current_user=current_user)
        assert exc.value.status_code == 400
        assert "46" in exc.value.detail

    def test_holiday_type_mismatch_propagates_400(self):
        """國定假日誤用 weekday 時應拋出 400"""
        from api.portal import overtimes as portal_ot

        session = _patched_session_ctx()
        emp = _patched_emp()
        data = _build_payload()
        current_user = {"username": "teacher1", "employee_id": 1}

        with (
            patch.object(portal_ot, "get_session", return_value=session),
            patch.object(portal_ot, "_get_employee", return_value=emp),
            patch(
                "api.overtimes._check_overtime_overlap", return_value=None
            ),
            patch(
                "api.overtimes.calculate_overtime_pay", return_value=400.0
            ),
            patch(
                "api.overtimes._check_monthly_overtime_cap"
            ),
            patch(
                "api.overtimes._check_overtime_type_calendar",
                side_effect=HTTPException(
                    status_code=400, detail="該日期為國定假日,加班類型須為 holiday"
                ),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                portal_ot.create_my_overtime(data=data, current_user=current_user)
        assert exc.value.status_code == 400
        assert "holiday" in exc.value.detail
