"""tests/test_attendance_guards.py — utils/attendance_guards 純函式測試。

涵蓋兩個 helper：
- require_not_self_attendance
- assert_no_self_in_batch
"""

import pytest
from fastapi import HTTPException

from utils.attendance_guards import (
    assert_no_self_in_batch,
    require_not_self_attendance,
)

# ── require_not_self_attendance ──────────────────────────────────────


class TestRequireNotSelfAttendance:
    def test_same_employee_id_raises_403(self):
        user = {"employee_id": 42}
        with pytest.raises(HTTPException) as exc:
            require_not_self_attendance(user, 42)
        assert exc.value.status_code == 403
        assert "自己的考勤" in exc.value.detail

    def test_different_employee_id_passes(self):
        user = {"employee_id": 42}
        require_not_self_attendance(user, 99)  # 不應 raise

    def test_pure_admin_no_employee_id_passes(self):
        # 純管理帳號 employee_id 為 None → 一律放行
        user = {"employee_id": None}
        require_not_self_attendance(user, 42)

    def test_missing_employee_id_key_passes(self):
        # 連 key 都沒有也視為純管理帳號
        require_not_self_attendance({}, 42)

    def test_custom_detail_message(self):
        user = {"employee_id": 1}
        with pytest.raises(HTTPException) as exc:
            require_not_self_attendance(user, 1, detail="自訂錯誤訊息")
        assert exc.value.detail == "自訂錯誤訊息"

    def test_string_vs_int_id_still_matches(self):
        # int(target) == int(caller) 比較，型別不同但值相同應視為自我
        user = {"employee_id": "7"}
        with pytest.raises(HTTPException):
            require_not_self_attendance(user, 7)


# ── assert_no_self_in_batch ──────────────────────────────────────────


class TestAssertNoSelfInBatch:
    def test_batch_contains_self_raises(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException) as exc:
            assert_no_self_in_batch(user, [1, 2, 5, 7])
        assert exc.value.status_code == 403
        assert "批次操作" in exc.value.detail

    def test_batch_without_self_passes(self):
        user = {"employee_id": 5}
        assert_no_self_in_batch(user, [1, 2, 3, 4])

    def test_pure_admin_passes_even_with_self_present(self):
        # 純管理帳號（無 employee_id）不檢查
        user = {"employee_id": None}
        assert_no_self_in_batch(user, [1, 2, 3])

    def test_empty_iterable_passes(self):
        user = {"employee_id": 5}
        assert_no_self_in_batch(user, [])

    def test_none_elements_ignored(self):
        user = {"employee_id": 5}
        assert_no_self_in_batch(user, [None, 1, None, 2])

    def test_non_int_castable_ids_skipped(self):
        # 無法 int() 的元素應靜默跳過，不可意外 raise
        user = {"employee_id": 5}
        assert_no_self_in_batch(user, ["abc", "xyz", None, 1])

    def test_string_id_matching_self_raises(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException):
            assert_no_self_in_batch(user, ["5"])

    def test_set_iterable_supported(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException):
            assert_no_self_in_batch(user, {1, 2, 5})

    def test_custom_detail_message(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException) as exc:
            assert_no_self_in_batch(user, [5], detail="自訂批次錯誤")
        assert exc.value.detail == "自訂批次錯誤"
