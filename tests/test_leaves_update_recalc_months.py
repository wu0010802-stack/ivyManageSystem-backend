"""Bug 回歸:已核准假單跨月更新只重算新月份(P2-3)。

Bug 描述:
    api/leaves.py:update_leave 在 line 754-756 雖記錄了 orig_month,但 line 837
    呼叫 _apply_leave_update_and_revoke 後,leave.start_date/end_date 已被改成
    新值。line 846 用 _collect_leave_months(leave.start_date, leave.end_date)
    收集要重算的月份,只會抓到新月份。orig_month 沒進集合。

    後果:假單從 3 月某天移到 4 月某天,4 月會重算正確,但 3 月的舊扣款
    依然停留在 3 月薪資,造成同一張假單被重複扣兩次月薪。

修復方向:
    months_to_recalc 須加上 orig_month。
"""

import sys
import os
import types
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_leave(leave_id=1, employee_id=10, start=date(2026, 3, 15), end=date(2026, 3, 15)):
    leave = types.SimpleNamespace()
    leave.id = leave_id
    leave.employee_id = employee_id
    leave.leave_type = "annual"
    leave.start_date = start
    leave.end_date = end
    leave.start_time = None
    leave.end_time = None
    leave.leave_hours = 8.0
    leave.deduction_ratio = 0.0
    leave.is_deductible = False
    leave.is_hospitalized = False
    leave.is_approved = True
    leave.approved_by = "admin"
    leave.rejection_reason = None
    leave.attachment_paths = None
    leave.substitute_status = "not_required"
    return leave


def _common_patches(leave, mock_engine):
    """套用 update_leave 所需的全部依賴 patch,讓焦點集中在月份重算。"""
    from api.leaves import _salary_engine as _orig_engine  # noqa: F401  (only to confirm symbol)

    session = MagicMock()
    # session.query(LeaveRecord).filter(...).first() → leave
    session.query.return_value.filter.return_value.first.return_value = leave

    return [
        patch("api.leaves.get_session", return_value=session),
        patch("api.leaves._check_overlap", return_value=None),
        patch("api.leaves.validate_leave_hours_against_schedule"),
        patch("api.leaves._check_leave_limits"),
        patch("api.leaves._guard_leave_quota"),
        patch("api.leaves._check_salary_months_not_finalized"),
        patch("api.leaves._salary_engine", mock_engine),
    ]


class TestUpdateLeaveCrossMonthRecalc:

    def test_cross_month_update_recalculates_orig_and_new_month(self):
        """已核准假單從 3/15 移到 4/15,薪資應對 3 月與 4 月皆重算"""
        from api.leaves import update_leave, LeaveUpdate

        leave = _make_leave(start=date(2026, 3, 15), end=date(2026, 3, 15))
        engine = MagicMock()

        patches = _common_patches(leave, engine)
        for p in patches:
            p.start()
        try:
            data = LeaveUpdate(start_date=date(2026, 4, 15), end_date=date(2026, 4, 15))
            update_leave(
                leave_id=leave.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        called_months = {
            (call.args[1], call.args[2]) for call in engine.process_salary_calculation.call_args_list
        }
        assert (2026, 3) in called_months, (
            f"3 月(orig)未被重算,實際呼叫:{called_months}"
        )
        assert (2026, 4) in called_months, (
            f"4 月(new)未被重算,實際呼叫:{called_months}"
        )

    def test_same_month_update_recalculates_once(self):
        """同月份更新(3/15 → 3/16)只需重算 3 月一次,不重複"""
        from api.leaves import update_leave, LeaveUpdate

        leave = _make_leave(start=date(2026, 3, 15), end=date(2026, 3, 15))
        engine = MagicMock()

        patches = _common_patches(leave, engine)
        for p in patches:
            p.start()
        try:
            data = LeaveUpdate(start_date=date(2026, 3, 16), end_date=date(2026, 3, 16))
            update_leave(
                leave_id=leave.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        called_months = [
            (call.args[1], call.args[2]) for call in engine.process_salary_calculation.call_args_list
        ]
        # 同一月份只應呼叫一次(set semantics)
        assert called_months == [(2026, 3)], f"期望僅 (2026, 3),實際:{called_months}"
