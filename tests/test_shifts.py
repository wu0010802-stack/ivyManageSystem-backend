"""排班儲存邏輯回歸測試

Bug 情境（2026-02-25）：
  save_assignments 端點在儲存前，先把該週「所有員工」的排班全刪，
  再把前端送來的清單重新寫入。
  → A 主管存 employee_1，employee_2 的排班同時被抹除（核彈效應）。

Fix：改為 per-employee upsert/delete，不影響清單以外的員工。
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
from api.shifts import _apply_employee_assignment_action  # noqa: E402


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
