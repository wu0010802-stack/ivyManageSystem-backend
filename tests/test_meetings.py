"""園務會議業務邏輯測試"""

from dataclasses import dataclass

import pytest


# ── 輕量 Mock：模擬 MeetingRecord ORM 物件（不需 DB）──────────────────
@dataclass
class MockMeetingRecord:
    """只保留本測試關心的欄位，供 duck-typing 呼叫。"""

    attended: bool
    overtime_hours: float
    overtime_pay: float


# ── 被測函式：從 api/meetings.py 匯入 ─────────────────────────────────
# 匯入真正的業務邏輯函式，確保測試對象是實際程式碼而非 mock
from api.meetings import _enforce_absent_no_overtime


# ─────────────────────────────────────────────────────────────────────
# 業務規則：缺席者加班費自動歸零
# ─────────────────────────────────────────────────────────────────────
class TestEnforceAbsentNoOvertime:
    """
    _enforce_absent_no_overtime(record) 業務規則：
    - attended=False → overtime_hours=0, overtime_pay=0
    - attended=True  → 保持原值不動
    """

    def test_absent_record_overtime_cleared(self):
        """缺席時，overtime_pay 與 overtime_hours 應歸零"""
        record = MockMeetingRecord(
            attended=False, overtime_hours=2.0, overtime_pay=200.0
        )
        _enforce_absent_no_overtime(record)
        assert record.overtime_pay == 0
        assert record.overtime_hours == 0

    def test_attended_record_overtime_preserved(self):
        """出席時，overtime_pay 與 overtime_hours 應保持不變"""
        record = MockMeetingRecord(
            attended=True, overtime_hours=2.0, overtime_pay=200.0
        )
        _enforce_absent_no_overtime(record)
        assert record.overtime_pay == 200.0
        assert record.overtime_hours == 2.0

    def test_absent_zero_overtime_stays_zero(self):
        """缺席且本來就是 0，呼叫後仍為 0（冪等）"""
        record = MockMeetingRecord(attended=False, overtime_hours=0, overtime_pay=0)
        _enforce_absent_no_overtime(record)
        assert record.overtime_pay == 0
        assert record.overtime_hours == 0


# ─────────────────────────────────────────────────────────────────────
# 回歸測試：update_meeting 邏輯（不啟動 HTTP，直接測業務規則組合）
# ─────────────────────────────────────────────────────────────────────
class TestUpdateMeetingBusinessRule:
    """
    回歸測試：管理員將 attended=True→False 時，
    幽靈加班費必須同步被清空。

    Bug 情境：
      - 老師原本出席（attended=True, overtime_pay=200）
      - 管理員只傳 attended=False，不傳 overtime_hours
      - Bug：overtime_pay 仍殘留 200 → 缺席者白領加班費
      - Fix：attended 改為 False 後，強制歸零
    """

    def _simulate_update(self, record, attended=None, overtime_hours=None):
        """模擬新版 update_meeting：只接受 attended / overtime_hours，pay 由後端控制。

        對應 handler 中 `_enforce_absent_no_overtime` 在欄位更新後的歸零守衛；
        本 simulate 不重算 pay，僅驗證歸零路徑（pay 重算邏輯走 _meeting_pay_for）。
        """
        if attended is not None:
            record.attended = attended
        if overtime_hours is not None:
            record.overtime_hours = overtime_hours
        # 業務規則：缺席者不得有加班費
        _enforce_absent_no_overtime(record)

    def test_change_attended_to_false_clears_overtime(self):
        """出席→缺席：加班費應被清空（幽靈加班費 bug 回歸）"""
        record = MockMeetingRecord(
            attended=True, overtime_hours=2.0, overtime_pay=200.0
        )
        self._simulate_update(record, attended=False)
        assert record.attended is False
        assert record.overtime_pay == 0
        assert record.overtime_hours == 0

    def test_update_remark_only_keeps_overtime_if_attended(self):
        """只改備註不改 attended：出席者加班費保持不變"""
        record = MockMeetingRecord(
            attended=True, overtime_hours=2.0, overtime_pay=200.0
        )
        self._simulate_update(record)  # attended/overtime_hours 都不傳
        assert record.overtime_pay == 200.0

    def test_send_attended_false_still_zeros_even_with_hours(self):
        """
        即使 client 送 attended=False + overtime_hours=2，
        業務規則仍應強制 attended=False 後 pay/hours 一律歸零
        """
        record = MockMeetingRecord(
            attended=True, overtime_hours=2.0, overtime_pay=200.0
        )
        self._simulate_update(record, attended=False, overtime_hours=2)
        assert record.overtime_pay == 0
        assert record.overtime_hours == 0


# ─────────────────────────────────────────────────────────────────────
# 新行為：overtime_pay 不再接受前端傳入；schema 應忽略此欄位
# ─────────────────────────────────────────────────────────────────────
class TestSchemaIgnoresOvertimePay:
    """確認 MeetingRecordCreate / MeetingRecordUpdate 不再接受 overtime_pay。

    Why: 拿掉前端 override 防止 MEETINGS 權限者塞超額金額繞過薪資簽核；
    Pydantic 預設 extra='ignore'，舊 client 仍可送但會被丟棄。
    """

    def test_create_schema_ignores_overtime_pay(self):
        from api.meetings import MeetingRecordCreate

        m = MeetingRecordCreate(
            employee_id=1,
            meeting_date="2026-04-01",
            overtime_hours=1.0,
            overtime_pay=99999,  # 應該被丟棄
        )
        assert (
            not hasattr(m, "overtime_pay") or getattr(m, "overtime_pay", None) is None
        )

    def test_update_schema_ignores_overtime_pay(self):
        from api.meetings import MeetingRecordUpdate

        m = MeetingRecordUpdate(overtime_pay=88888)
        assert (
            not hasattr(m, "overtime_pay") or getattr(m, "overtime_pay", None) is None
        )
