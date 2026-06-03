"""回歸：管理端 Excel 匯入請假時數驗證被自己的 except 吞掉 → 靜默變 8h。

Bug sweep 2026-06-03 ③：
  api/leaves._import_leaves_sync 內
      try:
          leave_hours = float(hours_raw)
          if leave_hours < 0.5:
              raise ValueError("時數至少 0.5 小時")
      except (ValueError, TypeError):
          leave_hours = 8.0
  其中 `<0.5` 的 raise 被同層 except 接住 → 該列改成 8.0（全日扣薪假），
  非法/非數字時數靜默變 8h pending，主管核准後依 8h 全額扣薪。

修後：非法時數（<0.5、非 0.5 倍數、>480、非數字）應計入 results['failed']，不建立。
"""

import asyncio
import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _build_parse_result(hours):
    from api.leaves import LeaveImportRow
    from utils.excel_io import ImportResult

    row = LeaveImportRow.model_validate(
        {
            "員工編號": "E001",
            "員工姓名": "員工 A",
            "假別代碼": "personal",
            "開始日期": "2026-03-15",
            "結束日期": "2026-03-15",
            "時數(可空)": hours,
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


def _run_import_with(hours):
    import api.leaves as leaves_module
    from api.leaves import import_leaves

    session = MagicMock()
    parse_result = _build_parse_result(hours)
    patches = [
        patch.object(leaves_module, "get_session", return_value=session),
        patch("api.leaves.read_upload_with_size_check", side_effect=_fake_read),
        patch("api.leaves.validate_file_signature"),
        patch("api.leaves.parse_excel", return_value=parse_result),
        patch("api.leaves.build_employee_lookup", return_value=({}, {})),
        patch("api.leaves.resolve_employee_from_row", return_value=_make_emp()),
        patch("api.leaves.validate_leave_hours_against_schedule"),
        patch("api.leaves._check_leave_limits"),
        patch("api.leaves._find_overlapping_leave", return_value=None),
        patch("api.leaves._check_quota"),
        patch("api.leaves._check_compensatory_quota"),
    ]
    for p in patches:
        p.start()
    try:
        return asyncio.run(
            import_leaves(file=MagicMock(), current_user={"username": "admin"})
        )
    finally:
        for p in patches:
            p.stop()


class TestImportHoursValidation:
    def test_sub_minimum_hours_row_fails_not_silent_8h(self):
        result = _run_import_with(0.2)
        assert result["created"] == 0, f"0.2h 列不應建立, result={result}"
        assert result["failed"] >= 1, f"0.2h 列應計入 failed, result={result}"

    def test_non_numeric_hours_row_fails(self):
        result = _run_import_with("abc")
        assert result["created"] == 0, f"非數字時數列不應建立, result={result}"
        assert result["failed"] >= 1, f"非數字時數列應計入 failed, result={result}"

    def test_non_half_multiple_hours_row_fails(self):
        result = _run_import_with(2.3)
        assert result["created"] == 0, f"2.3h（非 0.5 倍數）不應建立, result={result}"
        assert result["failed"] >= 1, f"2.3h 列應計入 failed, result={result}"

    def test_valid_hours_row_created(self):
        result = _run_import_with(4.0)
        assert result["created"] == 1, f"合法 4h 列應建立, result={result}"
        assert result["failed"] == 0, f"合法 4h 列不應 failed, result={result}"

    def test_empty_hours_defaults_to_full_day(self):
        # 時數留空 → 維持既有行為：預設 8h 全日
        result = _run_import_with(None)
        assert result["created"] == 1, f"空時數應預設 8h 建立, result={result}"
        assert result["failed"] == 0, f"空時數不應 failed, result={result}"
