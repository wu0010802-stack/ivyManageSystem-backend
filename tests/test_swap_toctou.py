"""
回歸測試：換班 TOCTOU（Time-of-Check to Time-of-Use）Race Condition

Bug 描述：
    POST /portal/swap-requests 發起換班時，會快照雙方當下班別，存入
    ShiftSwapRequest.requester_shift_type_id / target_shift_type_id。

    若主管在「申請後、B 同意前」修改 A 的班表，B 按下同意時，
    respond_swap_request 仍直接使用快照（已過期）來覆蓋 DailyShift，
    主管的修改被悄悄還原，班表莫名其妙回到舊狀態，且難以察覺。

修法：
    新增 _assert_swap_snapshot_fresh()，在接受換班前重新讀取雙方當前班別，
    若與快照不符，立即拋出 409 並將申請標記為 cancelled，
    要求重新發起（此時前端會讀到最新班表）。
"""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock
from fastapi import HTTPException


class TestAssertSwapSnapshotFresh:
    """_assert_swap_snapshot_fresh 輔助函式的單元測試"""

    def _make_swap(self, req_shift=1, tgt_shift=2):
        swap = MagicMock()
        swap.requester_id = 10
        swap.target_id = 20
        swap.swap_date = date(2025, 6, 15)
        swap.requester_shift_type_id = req_shift
        swap.target_shift_type_id = tgt_shift
        return swap

    def test_raises_409_when_requester_shift_changed(self):
        """申請後主管修改了 A 的班別 → 拋出 409 Conflict"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[3, 2]):   # A: 1→3 (changed), B: 2→2 (unchanged)
            with pytest.raises(HTTPException) as exc_info:
                _assert_swap_snapshot_fresh(MagicMock(), swap)

        assert exc_info.value.status_code == 409

    def test_raises_409_when_target_shift_changed(self):
        """申請後主管修改了 B 的班別 → 拋出 409 Conflict"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[1, 5]):   # A: 1→1 (unchanged), B: 2→5 (changed)
            with pytest.raises(HTTPException) as exc_info:
                _assert_swap_snapshot_fresh(MagicMock(), swap)

        assert exc_info.value.status_code == 409

    def test_raises_409_when_both_shifts_changed(self):
        """雙方班別都被修改 → 拋出 409"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[4, 5]):
            with pytest.raises(HTTPException) as exc_info:
                _assert_swap_snapshot_fresh(MagicMock(), swap)

        assert exc_info.value.status_code == 409

    def test_passes_when_both_shifts_unchanged(self):
        """快照與當前班別完全相符 → 不拋例外"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[1, 2]):
            _assert_swap_snapshot_fresh(MagicMock(), swap)  # must not raise

    def test_detail_guides_user_to_reapply(self):
        """409 訊息應引導員工重新申請，而非只說失敗"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[3, 2]):
            with pytest.raises(HTTPException) as exc_info:
                _assert_swap_snapshot_fresh(MagicMock(), swap)

        detail = exc_info.value.detail
        assert "過期" in detail or "修改" in detail

    def test_none_snapshot_and_none_current_is_fresh(self):
        """雙方原本都無班（None），快照與當前都是 None → 視為 fresh"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=None, tgt_shift=None)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[None, None]):
            _assert_swap_snapshot_fresh(MagicMock(), swap)  # must not raise

    def test_none_snapshot_but_shift_added_is_stale(self):
        """申請時 A 無班（None），主管後來新增班別 → 視為 stale，拋出 409"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=None, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[1, 2]):   # A: None→1 (admin added shift)
            with pytest.raises(HTTPException) as exc_info:
                _assert_swap_snapshot_fresh(MagicMock(), swap)

        assert exc_info.value.status_code == 409

    def test_reads_both_employees_exactly_once(self):
        """只查詢雙方各一次，不多查"""
        from api.portal.schedule import _assert_swap_snapshot_fresh

        swap = self._make_swap(req_shift=1, tgt_shift=2)

        with patch("api.portal.schedule._get_employee_shift_for_date",
                   side_effect=[1, 2]) as mock_get:
            _assert_swap_snapshot_fresh(MagicMock(), swap)

        assert mock_get.call_count == 2
        calls = mock_get.call_args_list
        called_emp_ids = [c.args[1] for c in calls]
        assert 10 in called_emp_ids   # requester_id
        assert 20 in called_emp_ids   # target_id
