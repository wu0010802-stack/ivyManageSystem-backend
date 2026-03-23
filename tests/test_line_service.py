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
    build_leave_result_message,
    build_overtime_result_message,
    build_salary_batch_message,
    build_dismissal_message,
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


class TestBuildMessages:
    def test_leave_result_approved(self):
        """核准的請假結果訊息包含已核准標記"""
        msg = build_leave_result_message("王小明", "事假", date(2026, 3, 1), date(2026, 3, 1), True)
        assert "已核准" in msg
        assert "王小明" in msg
        assert "事假" in msg

    def test_leave_result_rejected_with_reason(self):
        """駁回時訊息包含駁回原因"""
        msg = build_leave_result_message(
            "王小明", "事假", date(2026, 3, 1), date(2026, 3, 1), False, reason="資料不齊"
        )
        assert "已駁回" in msg
        assert "資料不齊" in msg

    def test_leave_result_rejected_no_reason(self):
        """駁回但無理由時，訊息中不出現 None"""
        msg = build_leave_result_message("王小明", "事假", date(2026, 3, 1), date(2026, 3, 1), False)
        assert "None" not in msg

    def test_overtime_result_approved(self):
        """核准的加班結果訊息"""
        msg = build_overtime_result_message("王小明", date(2026, 3, 1), "平日", True)
        assert "已核准" in msg
        assert "王小明" in msg

    def test_overtime_result_rejected(self):
        """駁回的加班結果訊息"""
        msg = build_overtime_result_message("王小明", date(2026, 3, 1), "假日", False)
        assert "已駁回" in msg

    def test_salary_batch(self):
        """薪資批次訊息包含年月人數金額"""
        msg = build_salary_batch_message(2026, 3, 12, 350000)
        assert "2026" in msg
        assert "3" in msg
        assert "12" in msg
        assert "350,000" in msg

    def test_dismissal_with_note(self):
        """接送通知包含備註"""
        msg = build_dismissal_message("小明", "大班甲", note="家長已在門口")
        assert "小明" in msg
        assert "大班甲" in msg
        assert "家長已在門口" in msg

    def test_dismissal_without_note(self):
        """接送通知無備註時不出現 None"""
        msg = build_dismissal_message("小明", "中班乙")
        assert "None" not in msg


class TestPushToUser:
    def test_push_to_user_calls_correct_url(self, monkeypatch):
        """_push_to_user 應呼叫 PUSH API 且 to 為 line_user_id"""
        svc = LineService()
        svc.configure("my-token", "group-id", True)

        captured = {}

        def mock_post(url, headers, json, timeout):
            captured["url"] = url
            captured["to"] = json["to"]
            resp = MagicMock()
            resp.status_code = 200
            return resp

        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        result = svc._push_to_user("Uabcd1234", "hello")
        assert result is True
        assert "push" in captured["url"]
        assert captured["to"] == "Uabcd1234"

    def test_push_disabled_returns_false(self):
        """服務未啟用時回傳 False"""
        svc = LineService()
        result = svc._push_to_user("Uabcd1234", "hello")
        assert result is False

    def test_push_to_user_no_token(self):
        """未設定 token 時回傳 False"""
        svc = LineService()
        svc._enabled = True
        result = svc._push_to_user("Uabcd1234", "hello")
        assert result is False

    def test_push_to_user_network_error(self, monkeypatch):
        """網路錯誤時回傳 False，不拋出"""
        svc = LineService()
        svc.configure("token", "group", True)
        monkeypatch.setattr(
            "services.line_service.requests.post",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("fail")),
        )
        assert svc._push_to_user("Uabcd1234", "test") is False


class TestReply:
    def test_reply_calls_reply_api(self, monkeypatch):
        """_reply 應呼叫 REPLY API"""
        svc = LineService()
        svc.configure("my-token", "group-id", True)

        captured = {}

        def mock_post(url, headers, json, timeout):
            captured["url"] = url
            captured["reply_token"] = json["replyToken"]
            resp = MagicMock()
            resp.status_code = 200
            return resp

        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        result = svc._reply("reply-abc", "hi")
        assert result is True
        assert "reply" in captured["url"]
        assert captured["reply_token"] == "reply-abc"

    def test_reply_no_token_returns_false(self):
        """未設定 token 時回傳 False"""
        svc = LineService()
        assert svc._reply("token", "msg") is False

    def test_reply_no_reply_token_returns_false(self):
        """reply_token 為空時回傳 False"""
        svc = LineService()
        svc.configure("my-token", "group", True)
        assert svc._reply("", "msg") is False


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

    def test_configure_updates_channel_secret(self):
        """configure() 傳入 channel_secret 時正確更新"""
        svc = LineService()
        svc.configure("t", "g", True, channel_secret="my-secret")
        assert svc._channel_secret == "my-secret"

    def test_configure_without_channel_secret_preserves_existing(self):
        """configure() 不傳 channel_secret 時保留既有值"""
        svc = LineService()
        svc._channel_secret = "existing-secret"
        svc.configure("t", "g", True)
        assert svc._channel_secret == "existing-secret"

    def test_push_returns_true_on_success(self, monkeypatch):
        """成功 push 時回傳 True"""
        svc = LineService()
        svc.configure("token", "target", True)

        mock_response = MagicMock()
        mock_response.status_code = 200

        monkeypatch.setattr("services.line_service.requests.post", lambda *a, **k: mock_response)
        result = svc._push("測試訊息")
        assert result is True
