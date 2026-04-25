"""Bug 回歸:重疊假單核准只警告不阻擋(P2-5,方案 B)。

Bug 描述:
    api/leaves.py:approve_leave 在 line 1004-1018 發現同員工已核准重疊假單時,
    只回 warning 仍核准;批次核准甚至完全沒這個檢查。
    結果同一員工同時段可同時存在多張已核准假單,造成重複扣薪、重複占配額。

修復方向(方案 B):
    - 單筆 ApproveRequest 新增 force_overlap: bool = False
    - 預設 force_overlap=False 時,_check_overlap 找到重疊 → 409 阻擋
    - force_overlap=True 時,降為 warning(主管確認後才強制過,記稽核日誌)
    - 批次核准無 force_overlap,一律硬擋(該筆計入 failed)
    - 批次須處理「同批兩張同員工同時段」邊角:後一張驗證時看得到前一張已 flush 為 approved
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
    leave.is_approved = None
    leave.approved_by = None
    leave.rejection_reason = None
    leave.attachment_paths = None
    leave.substitute_employee_id = None
    leave.substitute_status = "not_required"
    return leave


def _patches_for_approve(leave, conflict=None):
    """建立呼叫 approve_leave 所需 patch。conflict=None 代表無衝突。"""
    session = MagicMock()
    session.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = leave
    session.query.return_value.filter.return_value.first.return_value = None
    return session, [
        patch("api.leaves.get_session", return_value=session),
        patch("api.leaves._get_submitter_role", return_value="teacher"),
        patch("api.leaves._check_approval_eligibility", return_value=True),
        patch("api.leaves._check_substitute_guard"),
        patch("api.leaves._check_substitute_leave_conflict"),
        patch("api.leaves.validate_leave_hours_against_schedule"),
        patch("api.leaves._check_leave_limits"),
        patch("api.leaves._guard_leave_quota"),
        patch("api.leaves._check_salary_months_not_finalized"),
        patch("api.leaves._write_approval_log"),
        patch("api.leaves._salary_engine", None),  # 跳過薪資重算
        patch("api.leaves._check_overlap", return_value=conflict),
        patch(
            "services.leave_policy.requires_supporting_document", return_value=False
        ),
    ]


class TestSingleApproveOverlapBlock:

    def test_overlap_without_force_raises_409(self):
        """有已核准重疊 + force_overlap=False → 409 阻擋(原為 warning + 通過)"""
        from api.leaves import approve_leave, ApproveRequest

        leave = _make_leave()
        conflict = _make_leave(leave_id=99, start=date(2026, 3, 15), end=date(2026, 3, 15))
        conflict.is_approved = True

        session, patches = _patches_for_approve(leave, conflict=conflict)
        for p in patches:
            p.start()
        try:
            with pytest.raises(HTTPException) as exc:
                approve_leave(
                    leave_id=leave.id,
                    data=ApproveRequest(approved=True),
                    current_user={"username": "admin", "role": "supervisor"},
                )
            assert exc.value.status_code == 409
            assert "99" in exc.value.detail or "重疊" in exc.value.detail
        finally:
            for p in patches:
                p.stop()

    def test_overlap_with_force_returns_warning(self):
        """有重疊 + force_overlap=True → 通過但回 warning"""
        from api.leaves import approve_leave, ApproveRequest

        leave = _make_leave()
        conflict = _make_leave(leave_id=99, start=date(2026, 3, 15), end=date(2026, 3, 15))
        conflict.is_approved = True

        session, patches = _patches_for_approve(leave, conflict=conflict)
        for p in patches:
            p.start()
        try:
            result = approve_leave(
                leave_id=leave.id,
                data=ApproveRequest(approved=True, force_overlap=True),
                current_user={"username": "admin", "role": "supervisor"},
            )
            assert result.get("warning") is not None
            assert "重疊" in result["warning"] or "99" in result["warning"]
        finally:
            for p in patches:
                p.stop()

    def test_no_overlap_no_warning(self):
        """無重疊 → 通過、無 warning"""
        from api.leaves import approve_leave, ApproveRequest

        leave = _make_leave()
        session, patches = _patches_for_approve(leave, conflict=None)
        for p in patches:
            p.start()
        try:
            result = approve_leave(
                leave_id=leave.id,
                data=ApproveRequest(approved=True),
                current_user={"username": "admin", "role": "supervisor"},
            )
            assert result.get("warning") is None
        finally:
            for p in patches:
                p.stop()


class TestBatchApproveOverlapBlock:
    """批次核准遇到重疊一律硬擋,失敗條目進 failed list 不中斷批次。"""

    def _patches_for_batch(self, leave_map, conflict_map):
        """conflict_map: dict[leave_id] = conflict_record_or_None"""
        session = MagicMock()

        # session.query(LeaveRecord).filter(...).with_for_update().all() → leave list
        leave_records = list(leave_map.values())
        session.query.return_value.filter.return_value.with_for_update.return_value.all.return_value = leave_records

        # session.query(User.employee_id, User.role).filter(...).all() → []
        # 角色資格檢查我們直接 patch 跳過,所以這個查詢結果不影響

        return session, [
            patch("api.leaves.get_session", return_value=session),
            patch("api.leaves._check_approval_eligibility", return_value=True),
            patch("api.leaves._check_substitute_guard"),
            patch("api.leaves._check_substitute_leave_conflict"),
            patch("api.leaves.validate_leave_hours_against_schedule"),
            patch("api.leaves._check_leave_limits"),
            patch("api.leaves._guard_leave_quota"),
            patch("api.leaves._check_salary_months_not_finalized"),
            patch("api.leaves._write_approval_log"),
            patch("api.leaves._salary_engine", None),
            patch(
                "api.leaves._check_overlap",
                side_effect=lambda session, emp_id, start, end, st=None, et=None, exclude_id=None: conflict_map.get(
                    exclude_id
                ),
            ),
            patch(
                "services.leave_policy.requires_supporting_document",
                return_value=False,
            ),
            patch("api.leaves._batch_approve_limiter"),
        ]

    def test_overlap_record_marked_as_failed(self):
        """批次核准遇到重疊應記入 failed,不中斷其他正常條目"""
        from api.leaves import batch_approve_leaves, LeaveBatchApproveRequest

        ok = _make_leave(leave_id=1, employee_id=10)
        bad = _make_leave(leave_id=2, employee_id=20)
        leave_map = {1: ok, 2: bad}

        # leave 2 有重疊
        conflict = _make_leave(leave_id=88)
        conflict.is_approved = True
        conflict_map = {2: conflict, 1: None}

        session, patches = self._patches_for_batch(leave_map, conflict_map)
        for p in patches:
            p.start()
        try:
            req = LeaveBatchApproveRequest(ids=[1, 2], approved=True)
            result = batch_approve_leaves(
                data=req,
                _rl=None,
                current_user={"username": "admin", "role": "supervisor"},
            )
        finally:
            for p in patches:
                p.stop()

        assert 1 in result["succeeded"], f"無重疊的 leave 1 應成功:{result}"
        failed_ids = [f["id"] for f in result["failed"]]
        assert 2 in failed_ids, f"重疊的 leave 2 應失敗:{result}"
