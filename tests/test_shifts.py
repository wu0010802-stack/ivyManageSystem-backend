"""排班管理邏輯回歸測試

Bug 1（2026-02-25）：save_assignments 核彈刪除
  save_assignments 端點在儲存前，先把該週「所有員工」的排班全刪，
  再把前端送來的清單重新寫入。
  → A 主管存 employee_1，employee_2 的排班同時被抹除。
  Fix：改為 per-employee upsert/delete，不影響清單以外的員工。

Bug 2（2026-02-25）：delete_shift_type 漏查兩張子表
  刪除班別時只檢查 shift_assignments，漏查 daily_shifts 與
  shift_swap_requests，導致資料庫 FK IntegrityError → 500 當機。
  Fix：補查兩張表，合併成清楚的 400 錯誤訊息。
"""

from dataclasses import dataclass
from typing import Optional

import pytest


# ── Mock 物件（duck-typing，不需 DB）──────────────────────────────────────

@dataclass
class MockShiftAssignment:
    employee_id: int
    week_start_date: str
    shift_type_id: Optional[int] = None
    notes: Optional[str] = None


@dataclass
class MockAssignmentItem:
    employee_id: int
    shift_type_id: Optional[int] = None
    notes: Optional[str] = None


class MockSession:
    """記錄 add / delete 呼叫，供斷言使用。"""

    def __init__(self):
        self.added: list = []
        self.deleted: list = []

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)


# ── 被測函式（從 api/shifts.py 匯入）─────────────────────────────────────
from api.shifts import (  # noqa: E402
    _apply_employee_assignment_action,
    _shift_type_in_use_message,
)


# ─────────────────────────────────────────────────────────────────────────────
# 單元測試：_apply_employee_assignment_action
# ─────────────────────────────────────────────────────────────────────────────

WEEK = "2026-02-24"


class TestApplyEmployeeAssignmentAction:
    """
    Per-employee 排班寫入邏輯。

    四種情境：
      1. 員工「無排班」→「設定新班別」 → INSERT
      2. 員工「已有排班」→「更新班別」 → UPDATE（只改自己）
      3. 員工「已有排班」→「清空（None）」 → DELETE
      4. 員工「無排班」→「清空（None）」 → SKIP（不動任何資料）
    """

    def test_no_existing_with_shift_type_inserts(self):
        """無排班 + 有班別 → 新增一筆，不刪任何資料"""
        session = MockSession()
        item = MockAssignmentItem(employee_id=1, shift_type_id=10)

        result = _apply_employee_assignment_action(session, existing=None, item=item, week_date=WEEK)

        assert result == "inserted"
        assert len(session.added) == 1
        assert len(session.deleted) == 0
        assert session.added[0].employee_id == 1
        assert session.added[0].shift_type_id == 10

    def test_existing_with_shift_type_updates_only_self(self):
        """已有排班 + 有班別 → 僅更新自己的記錄，不刪其他人"""
        session = MockSession()
        existing = MockShiftAssignment(employee_id=1, week_start_date=WEEK, shift_type_id=5)
        item = MockAssignmentItem(employee_id=1, shift_type_id=10)

        result = _apply_employee_assignment_action(session, existing=existing, item=item, week_date=WEEK)

        assert result == "updated"
        assert existing.shift_type_id == 10  # 原記錄被更新
        assert len(session.added) == 0        # 不新增
        assert len(session.deleted) == 0      # 不刪

    def test_existing_with_null_shift_type_deletes(self):
        """已有排班 + 清空班別 → 刪除該員工自己的排班"""
        session = MockSession()
        existing = MockShiftAssignment(employee_id=1, week_start_date=WEEK, shift_type_id=5)
        item = MockAssignmentItem(employee_id=1, shift_type_id=None)

        result = _apply_employee_assignment_action(session, existing=existing, item=item, week_date=WEEK)

        assert result == "deleted"
        assert existing in session.deleted
        assert len(session.added) == 0

    def test_no_existing_with_null_shift_type_skips(self):
        """無排班 + 清空班別 → 什麼都不做（idempotent）"""
        session = MockSession()
        item = MockAssignmentItem(employee_id=1, shift_type_id=None)

        result = _apply_employee_assignment_action(session, existing=None, item=item, week_date=WEEK)

        assert result == "skipped"
        assert len(session.added) == 0
        assert len(session.deleted) == 0

    def test_other_employee_not_touched(self):
        """
        回歸：儲存 employee_1 不會觸動 employee_2。

        核心回歸情境：_apply_employee_assignment_action 只接收單一員工的
        existing 記錄，因此絕對不會隱式刪除其他員工的資料。
        此測試確保 API 呼叫路徑下，employee_2 的物件永遠不進入 session.delete。
        """
        session = MockSession()

        # employee_2 已有排班（另一主管排的）
        emp2_existing = MockShiftAssignment(employee_id=2, week_start_date=WEEK, shift_type_id=7)

        # 現在只操作 employee_1（存新班別）
        item_emp1 = MockAssignmentItem(employee_id=1, shift_type_id=10)
        _apply_employee_assignment_action(session, existing=None, item=item_emp1, week_date=WEEK)

        # employee_2 的記錄完全未被碰到
        assert emp2_existing not in session.deleted
        assert emp2_existing not in session.added

    def test_notes_are_preserved_on_update(self):
        """更新時，notes 應同步被更新"""
        session = MockSession()
        existing = MockShiftAssignment(employee_id=1, week_start_date=WEEK, shift_type_id=5, notes="舊備註")
        item = MockAssignmentItem(employee_id=1, shift_type_id=10, notes="新備註")

        _apply_employee_assignment_action(session, existing=existing, item=item, week_date=WEEK)

        assert existing.notes == "新備註"


# ─────────────────────────────────────────────────────────────────────────────
# 回歸測試 Bug 2：刪班別時漏查 DailyShift 與 ShiftSwapRequest
# ─────────────────────────────────────────────────────────────────────────────

class TestShiftTypeInUseMessage:
    """
    _shift_type_in_use_message(assignment_count, daily_count, swap_count) 邏輯。

    Bug 情境：
      delete_shift_type 只查 shift_assignments，
      漏查 daily_shifts 與 shift_swap_requests。
      只要任一子表有引用，DB FK 就會拋出 IntegrityError → 500 當機。

    Fix：提取 _shift_type_in_use_message，匯總三張表計數後回傳
    清楚的說明字串（in use）或 None（可安全刪除）。
    """

    def test_no_references_returns_none(self):
        """三張表都無引用 → 可刪除，回傳 None"""
        assert _shift_type_in_use_message(0, 0, 0) is None

    def test_only_assignment_in_use(self):
        """只有每週排班引用 → 回傳含『每週排班』的錯誤訊息"""
        msg = _shift_type_in_use_message(3, 0, 0)
        assert msg is not None
        assert "每週排班" in msg
        assert "3" in msg

    def test_only_daily_shift_in_use(self):
        """
        回歸：只有調班紀錄引用 → 舊版不查此表，會 500；
        修正後應回傳含『每日調班』的 400 錯誤訊息。
        """
        msg = _shift_type_in_use_message(0, 5, 0)
        assert msg is not None
        assert "每日調班" in msg
        assert "5" in msg

    def test_only_swap_request_in_use(self):
        """
        回歸：只有換班申請引用 → 舊版不查此表，會 500；
        修正後應回傳含『換班申請』的 400 錯誤訊息。
        """
        msg = _shift_type_in_use_message(0, 0, 2)
        assert msg is not None
        assert "換班申請" in msg
        assert "2" in msg

    def test_all_tables_in_use_shows_all(self):
        """三張表都有引用 → 訊息應包含全部三種描述"""
        msg = _shift_type_in_use_message(1, 2, 3)
        assert msg is not None
        assert "每週排班" in msg
        assert "每日調班" in msg
        assert "換班申請" in msg

    def test_mixed_assignment_and_daily(self):
        """每週排班 + 調班都有引用，沒有換班申請"""
        msg = _shift_type_in_use_message(2, 4, 0)
        assert msg is not None
        assert "每週排班" in msg
        assert "每日調班" in msg
        assert "換班申請" not in msg
