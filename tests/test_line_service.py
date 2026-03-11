"""
LINE 通知服務單元測試
"""

import pytest
from datetime import date
from unittest.mock import MagicMock, patch

from services.line_service import (
    LineService,
    build_leave_message,
    build_overtime_message,
)


class TestBuildLeaveMessage:
    def test_same_day(self):
        """同日假單只顯示單一日期，不顯示範圍"""
        msg = build_leave_message("王小明", "事假", date(2026, 3, 1), date(2026, 3, 1), 4)
        assert "2026-03-01" in msg
        assert "～" not in msg

    def test_multi_day(self):
        """跨日假單顯示日期範圍"""
        msg = build_leave_message("王小明", "年假", date(2026, 3, 1), date(2026, 3, 5), 32)
        assert "2026-03-01 ～ 2026-03-05" in msg

    def test_contains_hours(self):
        """訊息必須包含時數資訊"""
        msg = build_leave_message("陳大華", "病假", date(2026, 3, 1), date(2026, 3, 1), 8)
        assert "8h" in msg
        assert "病假" in msg
        assert "陳大華" in msg

    def test_contains_status(self):
        """訊息包含待審核狀態說明"""
        msg = build_leave_message("李美麗", "事假", date(2026, 3, 1), date(2026, 3, 1), 4)
        assert "待主管核准" in msg


class TestBuildOvertimeMessage:
    def test_comp_leave_tag(self):
        """use_comp=True 顯示「補休申請」"""
        msg = build_overtime_message("王小明", date(2026, 3, 1), "平日", 2, use_comp=True)
        assert "補休申請" in msg

    def test_overtime_tag(self):
        """use_comp=False 顯示「加班申請」"""
        msg = build_overtime_message("王小明", date(2026, 3, 1), "平日", 2, use_comp=False)
        assert "加班申請" in msg

    def test_contains_hours(self):
        """訊息包含時數資訊"""
        msg = build_overtime_message("陳大華", date(2026, 3, 15), "假日", 4, use_comp=False)
        assert "4h" in msg
        assert "陳大華" in msg

    def test_date_format_is_iso(self):
        """日期格式為 ISO（回歸測試：確保 ot_date/ot_type 參數順序正確）"""
        msg = build_overtime_message("王小明", date(2026, 3, 18), "平日", 1, use_comp=False)
        assert "2026-03-18" in msg
        assert "平日" in msg


class TestLineServiceSafety:
    def test_returns_false_when_not_configured(self):
        """未設定 token 或 target_id 時，_push 回傳 False，不拋出例外"""
        svc = LineService()
        result = svc._push("測試訊息")
        assert result is False

    def test_returns_false_when_disabled(self):
        """已設定但未啟用時，_push 回傳 False"""
        svc = LineService()
        svc.configure("some-token", "some-target", False)
        result = svc._push("測試訊息")
        assert result is False

    def test_does_not_raise_on_network_error(self, monkeypatch):
        """網路錯誤時 _push 回傳 False，不拋出例外"""
        svc = LineService()
        svc.configure("token", "target", True)

        def mock_post(*args, **kwargs):
            raise ConnectionError("網路異常")

        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        result = svc._push("測試")
        assert result is False

    def test_notify_leave_does_not_raise(self, monkeypatch):
        """notify_leave_submitted 即使推送失敗也不拋出例外"""
        svc = LineService()
        svc.configure("token", "target", True)

        monkeypatch.setattr("services.line_service.requests.post", lambda *a, **k: (_ for _ in ()).throw(Exception("error")))
        # 不應拋出
        svc.notify_leave_submitted("王小明", "事假", date(2026, 3, 1), date(2026, 3, 1), 4)

    def test_configure_updates_fields(self):
        """configure() 正確更新內部狀態"""
        svc = LineService()
        assert not svc._enabled
        svc.configure("my-token", "my-target", True)
        assert svc._token == "my-token"
        assert svc._target_id == "my-target"
        assert svc._enabled is True

    def test_push_returns_true_on_success(self, monkeypatch):
        """成功 push 時回傳 True"""
        svc = LineService()
        svc.configure("token", "target", True)

        mock_response = MagicMock()
        mock_response.status_code = 200

        monkeypatch.setattr("services.line_service.requests.post", lambda *a, **k: mock_response)
        result = svc._push("測試訊息")
        assert result is True
