"""LineService 家長端通知方法的純函式樣板測試。

不打 LINE API，僅驗證訊息字串內容。
"""

from datetime import date

from services.line_service import (
    LineService,
    _build_parent_leave_result_message,
)


class FakeLineService(LineService):
    """攔截 _push_to_user，記錄目標與訊息。"""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def _push_to_user(self, line_user_id: str, text: str) -> bool:  # type: ignore[override]
        self.calls.append((line_user_id, text))
        return True


class TestParentLeaveResultMessage:
    def test_approved_includes_status(self):
        msg = _build_parent_leave_result_message(
            "小明", "病假", date(2026, 4, 22), date(2026, 4, 22), True, None
        )
        assert "已核准" in msg
        assert "小明" in msg
        assert "病假" in msg

    def test_rejected_includes_status(self):
        msg = _build_parent_leave_result_message(
            "小華", "事假", date(2026, 4, 22), date(2026, 4, 24), False, "證明不足"
        )
        assert "未核准" in msg
        assert "證明不足" in msg
        assert "2026-04-22~2026-04-24" in msg


class TestNotifyMethods:
    def test_notify_parent_leave_result_calls_push_to_user(self):
        svc = FakeLineService()
        svc.configure(token="dummy", target_id="dummy", enabled=True)
        svc.notify_parent_leave_result(
            "U001",
            "小明",
            "病假",
            date(2026, 4, 22),
            date(2026, 4, 22),
            approved=True,
        )
        assert len(svc.calls) == 1
        line_id, text = svc.calls[0]
        assert line_id == "U001"
        assert "小明" in text

    def test_notify_parent_attendance_alert(self):
        svc = FakeLineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc.notify_parent_attendance_alert("U", "小明", date(2026, 4, 22), "缺席")
        line_id, text = svc.calls[0]
        assert "缺席" in text

    def test_notify_parent_announcement_truncates_long_preview(self):
        svc = FakeLineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc.notify_parent_announcement("U", "重要通知", "x" * 100)
        _, text = svc.calls[0]
        assert "重要通知" in text
        assert "…" in text  # 長文截斷

    def test_notify_parent_fee_due(self):
        svc = FakeLineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc.notify_parent_fee_due("U", "小明", "學費", 10000, date(2026, 5, 1))
        _, text = svc.calls[0]
        assert "$10000" in text
        assert "2026-05-01" in text

    def test_notify_parent_event_ack_required(self):
        svc = FakeLineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc.notify_parent_event_ack_required("U", "親師懇談", date(2026, 5, 10))
        _, text = svc.calls[0]
        assert "親師懇談" in text
        assert "2026-05-10" in text

    def test_disabled_service_returns_silently(self):
        """enabled=False 時 _push_to_user 應返回 False 但不拋（fail-safe）。"""
        svc = LineService()  # 預設 enabled=False
        # 直接呼叫底層方法應該返回 False；notify_* 為 fail-safe wrapper
        svc.notify_parent_leave_result(
            "U", "小明", "病假", date(2026, 4, 22), date(2026, 4, 22), approved=True
        )
        # 沒拋例外即通過
