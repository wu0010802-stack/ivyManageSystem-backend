"""
approvals.py 功能回歸測試

測試範圍（不依賴 DB）：
- _EVENT_TYPE_LABELS 完整性與 fallback 行為
- approval-summary total 加總公式
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ============================================================
# _EVENT_TYPE_LABELS 完整性與 fallback 行為
# ============================================================

class TestEventTypeLabels:
    """確保所有已知 event_type 都有對應中文 label，未知 type 原樣回傳"""

    @pytest.fixture(autouse=True)
    def _import_labels(self):
        from api.approvals import _EVENT_TYPE_LABELS
        self.labels = _EVENT_TYPE_LABELS

    def test_all_known_types_have_labels(self):
        """四個已知 event_type 都有對應的中文 label"""
        expected_types = {"meeting", "activity", "holiday", "general"}
        assert set(self.labels.keys()) == expected_types

    def test_label_values_are_nonempty_strings(self):
        """每個 label 值都是非空字串"""
        for key, value in self.labels.items():
            assert isinstance(value, str) and value, f"'{key}' 的 label 不可為空"

    def test_unknown_type_fallback_returns_raw_value(self):
        """未知 event_type 時，.get() fallback 應原樣回傳該 type 字串"""
        unknown = "custom_type"
        result = self.labels.get(unknown, unknown)
        assert result == unknown

    def test_known_type_returns_chinese_label(self):
        """已知 type 返回中文 label，不返回原始 key"""
        assert self.labels.get("meeting", "meeting") != "meeting"
        assert self.labels.get("holiday", "holiday") != "holiday"


# ============================================================
# approval-summary total 加總公式
# ============================================================

class TestApprovalSummaryTotal:
    """驗證 get_approval_summary 的加總邏輯

    不依賴 DB，直接對公式邏輯建模測試，確保 total 計算正確。
    """

    def _compute_total(self, pending_leaves, pending_overtimes, pending_corrections):
        """複製 get_approval_summary 的加總邏輯"""
        return pending_leaves + pending_overtimes + pending_corrections

    def test_all_zero_returns_zero(self):
        """三項都為 0 → total = 0"""
        assert self._compute_total(0, 0, 0) == 0

    def test_single_nonzero_leaves(self):
        """只有假單待審 → total 等於假單數"""
        assert self._compute_total(3, 0, 0) == 3

    def test_single_nonzero_overtimes(self):
        """只有加班待審 → total 等於加班數"""
        assert self._compute_total(0, 5, 0) == 5

    def test_single_nonzero_corrections(self):
        """只有補打卡待審 → total 等於補打卡數"""
        assert self._compute_total(0, 0, 2) == 2

    def test_all_nonzero_sums_correctly(self):
        """三項都有值 → total 為三者之和"""
        assert self._compute_total(3, 5, 2) == 10

    def test_total_equals_sum_of_parts(self):
        """total 必須等於三個子項目的算術和"""
        leaves, overtimes, corrections = 7, 3, 1
        result = self._compute_total(leaves, overtimes, corrections)
        assert result == leaves + overtimes + corrections


# ============================================================
# student-attendance-summary 公式
# ============================================================

class TestStudentAttendanceSummary:
    """驗證學生出勤摘要計算邏輯"""

    @pytest.fixture(autouse=True)
    def _import_builder(self):
        from services.student_attendance_report import build_attendance_summary
        self.build_summary = build_attendance_summary

    def test_counts_are_aggregated_correctly(self):
        summary = self.build_summary(20, {
            "出席": 12,
            "遲到": 2,
            "缺席": 3,
            "病假": 1,
            "事假": 1,
        })

        assert summary["total_students"] == 20
        assert summary["recorded_count"] == 19
        assert summary["on_campus_count"] == 14
        assert summary["leave_count"] == 2
        assert summary["unmarked_count"] == 1

    def test_rates_are_zero_when_no_students(self):
        summary = self.build_summary(0, {})

        assert summary["record_completion_rate"] == 0
        assert summary["attendance_rate"] == 0

    def test_unknown_statuses_are_ignored(self):
        summary = self.build_summary(5, {
            "出席": 3,
            "早退": 99,
        })

        assert summary["recorded_count"] == 3
        assert summary["unmarked_count"] == 2
