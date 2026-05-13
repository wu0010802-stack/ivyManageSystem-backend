"""Bug 回歸:管理端 Excel 匯入也繞過補休配額(P1-1 第三個出口)。

api/leaves.py:import_leaves 直接呼叫 _check_quota,
對 compensatory 因不在 QUOTA_LEAVE_TYPES 而 silent return。
HR 可用 Excel 大量匯入超過配額的補休假單,主管核准後直接扣薪/占配額。

修復方向:對 compensatory 走 _check_compensatory_quota,其他維持 _check_quota。

2026-05-13 Task 8 後：endpoint 改用 utils.excel_io.parse_excel；
本測試改 patch `api.leaves.parse_excel` 回傳預先建構的 LeaveImportRow，
原 pd.read_excel patch 已無效（endpoint 不再呼叫 pandas）。
"""

import sys
import os
import asyncio
import types
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _build_parse_result(leave_type="compensatory"):
    """模擬 parse_excel 回傳：包一筆 LeaveImportRow（無 parse 錯誤）。"""
    from api.leaves import LeaveImportRow
    from utils.excel_io import ImportResult

    row = LeaveImportRow.model_validate(
        {
            "員工編號": "E001",
            "員工姓名": "員工 A",
            "假別代碼": leave_type,
            "開始日期": "2026-03-15",
            "結束日期": "2026-03-15",
            "時數(可空)": 4.0,
            "原因(可空)": "匯入測試",
        }
    )
    return ImportResult(rows=[row], errors=[])


def _make_emp():
    emp = types.SimpleNamespace()
    emp.id = 10
    emp.name = "員工 A"
    return emp


async def _fake_read(_f):
    return b""


def _common_patches(emp, parse_result):
    """為 import_leaves 準備所有外部依賴 patch。"""
    import api.leaves as leaves_module

    session = MagicMock()
    return [
        patch.object(leaves_module, "get_session", return_value=session),
        patch("api.leaves.read_upload_with_size_check", side_effect=_fake_read),
        patch("api.leaves.validate_file_signature"),
        # 2026-05-13 Task 8：endpoint 改用 utils.excel_io.parse_excel
        patch("api.leaves.parse_excel", return_value=parse_result),
        patch("api.leaves.build_employee_lookup", return_value=({}, {})),
        patch("api.leaves.resolve_employee_from_row", return_value=emp),
        patch("api.leaves.validate_leave_hours_against_schedule"),
        patch("api.leaves._check_leave_limits"),
        # 2026-05-11 P1-4：import_leaves 新增 _find_overlapping_leave；
        # mock 回 None 避免假衝突中斷 dispatch 測試。
        patch("api.leaves._find_overlapping_leave", return_value=None),
    ]


def _run_import(file_mock):
    """呼叫 async import_leaves 並回傳結果。"""
    from api.leaves import import_leaves

    return asyncio.run(
        import_leaves(file=file_mock, current_user={"username": "admin"})
    )


class TestImportLeavesCompensatoryDispatch:

    def test_compensatory_row_dispatched_to_compensatory_helper(self):
        """import 含 compensatory 列時應呼叫 _check_compensatory_quota,而非 _check_quota"""
        import api.leaves as leaves_module

        emp = _make_emp()
        parse_result = _build_parse_result(leave_type="compensatory")

        patches = _common_patches(emp, parse_result)

        for p in patches:
            p.start()
        try:
            file_mock = MagicMock()
            with (
                patch.object(leaves_module, "_check_compensatory_quota") as mock_comp,
                patch.object(leaves_module, "_check_quota") as mock_quota,
            ):
                result = _run_import(file_mock)
            assert mock_comp.called, (
                f"import 對 compensatory 列未呼叫 _check_compensatory_quota,"
                f"errors={result.get('errors')}"
            )
            assert (
                not mock_quota.called
            ), f"compensatory 不應再走 _check_quota,errors={result.get('errors')}"
        finally:
            for p in patches:
                p.stop()

    def test_non_compensatory_row_still_uses_check_quota(self):
        """非 compensatory 列仍走 _check_quota(維持原行為)"""
        import api.leaves as leaves_module

        emp = _make_emp()
        parse_result = _build_parse_result(leave_type="annual")
        patches = _common_patches(emp, parse_result)

        for p in patches:
            p.start()
        try:
            file_mock = MagicMock()
            with (
                patch.object(leaves_module, "_check_compensatory_quota") as mock_comp,
                patch.object(leaves_module, "_check_quota") as mock_quota,
            ):
                _run_import(file_mock)
            assert mock_quota.called, "annual 列應呼叫 _check_quota"
            assert not mock_comp.called, "annual 列不應呼叫 _check_compensatory_quota"
        finally:
            for p in patches:
                p.stop()
