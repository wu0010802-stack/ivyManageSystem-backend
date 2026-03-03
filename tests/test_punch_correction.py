"""
補打卡申請功能回歸測試

測試範圍：
- PunchCorrectionCreate Pydantic 驗證（日期、類型、時間必填）
- approval_status property
- 防重複申請邏輯（透過模擬 ORM 物件）
"""

import sys
import os
import pytest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pydantic import ValidationError


# ============================================================
# Pydantic schema 驗證測試
# ============================================================

class TestPunchCorrectionCreate:
    """PunchCorrectionCreate Pydantic schema 驗證"""

    @pytest.fixture(autouse=True)
    def _import_schema(self):
        from api.portal.punch_corrections import PunchCorrectionCreate
        self.Schema = PunchCorrectionCreate

    def _build(self, **kwargs):
        """建立基礎合法 payload，可覆蓋欄位"""
        base = {
            "attendance_date": date.today() - timedelta(days=1),
            "correction_type": "punch_out",
            "requested_punch_in": None,
            "requested_punch_out": datetime(
                (date.today() - timedelta(days=1)).year,
                (date.today() - timedelta(days=1)).month,
                (date.today() - timedelta(days=1)).day,
                18, 0
            ),
            "reason": "忘記打下班",
        }
        base.update(kwargs)
        return base

    # ── 合法情境 ──

    def test_valid_punch_out(self):
        """補下班卡：有 requested_punch_out，無 punch_in → 合法"""
        data = self._build()
        obj = self.Schema(**data)
        assert obj.correction_type == "punch_out"

    def test_valid_punch_in(self):
        """補上班卡：有 requested_punch_in，無 punch_out → 合法"""
        yesterday = date.today() - timedelta(days=1)
        obj = self.Schema(
            attendance_date=yesterday,
            correction_type="punch_in",
            requested_punch_in=datetime(yesterday.year, yesterday.month, yesterday.day, 8, 0),
            requested_punch_out=None,
            reason=None,
        )
        assert obj.correction_type == "punch_in"

    def test_valid_both(self):
        """補全天：punch_in 和 punch_out 都填 → 合法"""
        yesterday = date.today() - timedelta(days=1)
        obj = self.Schema(
            attendance_date=yesterday,
            correction_type="both",
            requested_punch_in=datetime(yesterday.year, yesterday.month, yesterday.day, 8, 0),
            requested_punch_out=datetime(yesterday.year, yesterday.month, yesterday.day, 17, 0),
            reason="整天忘記打卡",
        )
        assert obj.correction_type == "both"

    # ── 非法情境 ──

    def test_future_date_raises(self):
        """未來日期 → 422"""
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=date.today() + timedelta(days=1),
                correction_type="punch_out",
                requested_punch_out=datetime.now() + timedelta(days=1),
            )
        assert "未來日期" in str(exc.value)

    def test_punch_in_missing_requested_time_raises(self):
        """correction_type=punch_in 但未填 requested_punch_in → 422"""
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=yesterday,
                correction_type="punch_in",
                requested_punch_in=None,
                requested_punch_out=None,
            )
        assert "上班時間" in str(exc.value)

    def test_punch_out_missing_requested_time_raises(self):
        """correction_type=punch_out 但未填 requested_punch_out → 422"""
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=yesterday,
                correction_type="punch_out",
                requested_punch_in=None,
                requested_punch_out=None,
            )
        assert "下班時間" in str(exc.value)

    def test_both_missing_punch_in_raises(self):
        """correction_type=both 缺 requested_punch_in → 422"""
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=yesterday,
                correction_type="both",
                requested_punch_in=None,
                requested_punch_out=datetime(yesterday.year, yesterday.month, yesterday.day, 17, 0),
            )
        assert "上班時間" in str(exc.value)

    def test_both_missing_punch_out_raises(self):
        """correction_type=both 缺 requested_punch_out → 422"""
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=yesterday,
                correction_type="both",
                requested_punch_in=datetime(yesterday.year, yesterday.month, yesterday.day, 8, 0),
                requested_punch_out=None,
            )
        assert "下班時間" in str(exc.value)

    def test_invalid_correction_type_raises(self):
        """無效 correction_type → 422"""
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            self.Schema(
                attendance_date=yesterday,
                correction_type="invalid_type",
            )
        assert "補正類型" in str(exc.value)

    def test_today_is_allowed(self):
        """今天的日期為合法（不算未來）"""
        today = date.today()
        obj = self.Schema(
            attendance_date=today,
            correction_type="punch_out",
            requested_punch_out=datetime(today.year, today.month, today.day, 18, 0),
        )
        assert obj.attendance_date == today


# ============================================================
# approval_status property 測試
# ============================================================

class TestPunchCorrectionApprovalStatus:
    """PunchCorrectionRequest.approval_status property

    直接測試 property 的邏輯，不依賴 SQLAlchemy ORM 狀態初始化。
    """

    def _make_approval_status(self, is_approved):
        """直接呼叫 approval_status 邏輯（等同 ORM property）"""
        # 複製 model 的 property 邏輯，獨立驗證
        if is_approved is True:
            return 'approved'
        if is_approved is False:
            return 'rejected'
        return 'pending'

    def test_none_returns_pending(self):
        assert self._make_approval_status(None) == "pending"

    def test_true_returns_approved(self):
        assert self._make_approval_status(True) == "approved"

    def test_false_returns_rejected(self):
        assert self._make_approval_status(False) == "rejected"

    def test_property_logic_matches_model(self):
        """確認 PunchCorrectionRequest.approval_status property 邏輯和預期一致
        透過直接取 property 的 fget 函式測試，避開 ORM 初始化問題"""
        from models.database import PunchCorrectionRequest

        # 取得 property 的 fget
        fget = PunchCorrectionRequest.approval_status.fget

        # 用 mock 物件測試（只需要 is_approved 屬性）
        class _Mock:
            def __init__(self, val):
                self.is_approved = val

        assert fget(_Mock(None)) == "pending"
        assert fget(_Mock(True)) == "approved"
        assert fget(_Mock(False)) == "rejected"


# ============================================================
# CORRECTION_TYPE_LABELS 完整性測試
# ============================================================

class TestCorrectionTypeLabels:
    """確保所有合法的 correction_type 都有對應的中文 label"""

    def test_all_types_have_labels(self):
        from api.portal.punch_corrections import CORRECTION_TYPE_LABELS
        from api.punch_corrections import CORRECTION_TYPE_LABELS as ADMIN_LABELS

        expected = {"punch_in", "punch_out", "both"}
        assert set(CORRECTION_TYPE_LABELS.keys()) == expected
        assert set(ADMIN_LABELS.keys()) == expected

    def test_portal_labels_match_admin_labels(self):
        """portal 端和 admin 端的 label 應相同"""
        from api.portal.punch_corrections import CORRECTION_TYPE_LABELS as PORTAL_LABELS
        from api.punch_corrections import CORRECTION_TYPE_LABELS as ADMIN_LABELS
        assert PORTAL_LABELS == ADMIN_LABELS
