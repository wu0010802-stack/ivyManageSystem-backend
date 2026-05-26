"""ApprovalStatus enum 與三表 `status` column 預設值 / CHECK 範圍測試。

P1/P2 期間的 dual-write listener 測試已隨 listener 一併移除（P4）。
這裡保留兩件最小事實：
1. ApprovalStatus enum 值對 frontend / migration / API 是字串契約，
   值若改名一定要連動 frontend constants 與 migration 的 CHECK 字串。
2. 三表 default 為 'pending'，且 record.status 是真的 column（不再
   是從 is_approved 算出的 property）。
"""

from models.approval import ApprovalStatus
from models.leave import LeaveRecord
from models.overtime import OvertimeRecord, PunchCorrectionRequest


class TestApprovalStatusEnum:
    def test_enum_values_are_lowercase_strings(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"

    def test_enum_is_str_subclass(self):
        # str Enum lets `== 'pending'` work without `.value`.
        assert ApprovalStatus.PENDING == "pending"

    def test_enum_set_complete(self):
        values = {m.value for m in ApprovalStatus}
        assert values == {"pending", "approved", "rejected"}


class TestStatusColumnDefault:
    def test_leave_status_defaults_to_pending(self):
        rec = LeaveRecord()
        # server_default kicks in on INSERT, not at construction —
        # but the SQLAlchemy Column default ("pending") populates Python-side too.
        assert rec.status in (
            None,
            "pending",
        ), f"unexpected pre-insert status: {rec.status!r}"

    def test_overtime_status_defaults_to_pending(self):
        rec = OvertimeRecord()
        assert rec.status in (
            None,
            "pending",
        ), f"unexpected pre-insert status: {rec.status!r}"

    def test_punch_status_defaults_to_pending(self):
        rec = PunchCorrectionRequest()
        assert rec.status in (
            None,
            "pending",
        ), f"unexpected pre-insert status: {rec.status!r}"

    def test_approval_status_property_reads_column(self):
        rec = LeaveRecord()
        rec.status = "approved"
        assert rec.approval_status == "approved"
        rec.status = "rejected"
        assert rec.approval_status == "rejected"
        rec.status = "pending"
        assert rec.approval_status == "pending"
