"""DEPRECATED: 原本測 LineService._notify_parent_* 7 個家長通知 method。
Phase 4 Section 4 (2026-05-26) 落地後 _notify_* 全部從 line_service.py 移除，
家長推送邏輯由 services/notification/_channels/line.py LINE_HANDLERS dict 取代。
驗證見 tests/notification/test_channels_line.py（含 parent.message_received
quick-reply、_h_parent_announcement、_h_parent_contact_book_published 等）。

`_build_parent_leave_result_message` 純函式仍在（被 LINE_HANDLERS 用）：保留一個
sanity test 驗訊息內容；其他 _notify_* 行為由 LINE_HANDLERS test 覆蓋。

本檔保留為 phase-4 退役歷史 placeholder，下個 minor version 可移除。
"""

from datetime import date

from services.line_service import _build_parent_leave_result_message


def test_build_parent_leave_result_message_approved():
    msg = _build_parent_leave_result_message(
        student_name="小明",
        leave_type="病假",
        start=date(2026, 5, 1),
        end=date(2026, 5, 1),
        approved=True,
    )
    assert "小明" in msg
    assert "病假" in msg
    assert "2026-05-01" in msg
    assert "已核准" in msg


def test_build_parent_leave_result_message_rejected_with_note():
    msg = _build_parent_leave_result_message(
        student_name="小華",
        leave_type="事假",
        start=date(2026, 5, 1),
        end=date(2026, 5, 3),
        approved=False,
        review_note="證明文件不足",
    )
    assert "小華" in msg
    assert "事假" in msg
    assert "2026-05-01~2026-05-03" in msg
    assert "未核准" in msg
    assert "證明文件不足" in msg


def test_notify_parent_methods_retired_in_phase4_section4():
    """Sanity check: 7 個 _notify_parent_* 已從 LineService class 移除。"""
    from services.line_service import LineService

    retired_methods = [
        "_notify_parent_message_received",
        "_notify_parent_leave_result",
        "_notify_parent_attendance_alert",
        "_notify_parent_announcement",
        "_notify_parent_fee_due",
        "_notify_parent_event_ack_required",
        "_notify_parent_contact_book_published",
    ]
    for name in retired_methods:
        assert not hasattr(
            LineService, name
        ), f"{name} 應已退役 (Phase 4 Section 4)；推送由 LINE_HANDLERS 取代。"
