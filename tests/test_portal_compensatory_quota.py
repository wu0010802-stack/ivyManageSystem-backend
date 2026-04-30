"""Bug 回歸:Portal 教師端建立補休假單也繞過配額檢查(P1-1 補丁)。

api/portal/leaves.py 直接呼叫 _check_quota,不認識 compensatory(_check_quota
對非 QUOTA_LEAVE_TYPES 直接 return)。教師可從 portal 送出超過累積配額的補休,
甚至完全沒累積過配額也能送。

修復方向:portal create_my_leave 對 compensatory 走 _check_compensatory_quota。
"""

import sys
import os
import types
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_emp():
    emp = types.SimpleNamespace()
    emp.id = 10
    emp.name = "測試教師"
    emp.base_salary = 30000
    emp.hire_date = date(2020, 1, 1)
    return emp


class TestPortalCreateLeaveCompensatoryQuota:
    """portal 對 compensatory 必須呼叫 _check_compensatory_quota,不可走 _check_quota。"""

    def _build_payload(self):
        from api.portal._shared import LeaveCreatePortal

        return LeaveCreatePortal(
            leave_type="compensatory",
            start_date=date(2026, 3, 15),
            end_date=date(2026, 3, 15),
            leave_hours=4.0,
            reason="補休",
        )

    def _common_patches(self, emp):
        from api.portal import leaves as portal_lv

        session = MagicMock()
        return session, [
            patch.object(portal_lv, "get_session", return_value=session),
            patch.object(portal_lv, "_get_employee", return_value=emp),
            patch.object(portal_lv, "_check_overlap", return_value=None),
            patch.object(portal_lv, "_check_substitute_leave_conflict"),
            patch.object(portal_lv, "validate_leave_hours_against_schedule"),
            patch.object(portal_lv, "_check_leave_limits"),
            patch.object(portal_lv, "validate_portal_leave_rules"),
        ]

    def test_compensatory_dispatched_to_compensatory_helper(self):
        """portal 對 compensatory 必須呼叫 _check_compensatory_quota"""
        from api.portal import leaves as portal_lv

        emp = _make_emp()
        session, patches = self._common_patches(emp)
        for p in patches:
            p.start()
        try:
            with (
                patch.object(portal_lv, "_check_compensatory_quota") as mock_comp,
                patch.object(portal_lv, "_check_quota") as mock_quota,
            ):
                # _check_compensatory_quota 通過 → 後續 add/commit
                try:
                    portal_lv.create_my_leave(
                        data=self._build_payload(),
                        request=MagicMock(),
                        current_user={"username": "teacher", "employee_id": 10},
                    )
                except Exception:
                    # 後續 ORM/commit 可能失敗,但配額分流必須先發生
                    pass
            assert (
                mock_comp.called
            ), "portal 對 compensatory 未呼叫 _check_compensatory_quota"
            assert not mock_quota.called, "portal 對 compensatory 不應再走 _check_quota"
        finally:
            for p in patches:
                p.stop()

    def test_no_compensatory_quota_blocks_request(self):
        """portal 申請補休但無配額累積 → 應 400"""
        from api.portal import leaves as portal_lv

        emp = _make_emp()
        session, patches = self._common_patches(emp)
        # 模擬:LeaveQuota 不存在(從未累積過補休)
        session.query.return_value.filter.return_value.first.return_value = None

        # _get_approved/pending hours 都回 0
        for p in patches:
            p.start()
        try:
            with (
                patch("api.leaves_quota._get_approved_hours_in_year", return_value=0.0),
                patch("api.leaves_quota._get_pending_hours_in_year", return_value=0.0),
            ):
                with pytest.raises(HTTPException) as exc:
                    portal_lv.create_my_leave(
                        data=self._build_payload(),
                        request=MagicMock(),
                        current_user={"username": "teacher", "employee_id": 10},
                    )
            assert exc.value.status_code == 400
            assert "補休" in exc.value.detail
        finally:
            for p in patches:
                p.stop()
