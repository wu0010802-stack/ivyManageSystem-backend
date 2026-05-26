"""channel matrix 覆蓋 + 順序測試。"""

from services.notification.event_types import NOTIFICATION_EVENT_TYPES
from services.notification.channel_matrix import CHANNEL_MATRIX, Channel


def test_channel_matrix_covers_all_event_types():
    """每個 event_type 必須有 matrix 對映。"""
    missing = NOTIFICATION_EVENT_TYPES - set(CHANNEL_MATRIX.keys())
    assert not missing, f"matrix 漏配 event_type: {missing}"


def test_channel_matrix_no_extra_keys():
    """matrix 不應有 event_types 沒列的 key（防 typo）。"""
    extra = set(CHANNEL_MATRIX.keys()) - NOTIFICATION_EVENT_TYPES
    assert not extra, f"matrix 有 event_types 未定義的 key: {extra}"


def test_employee_events_default_in_app_and_line():
    """員工域（不含 dismissal、不含家長推播的 activity.waitlist_*/growth_report.*）
    預設 (in_app, line)。"""
    parent_targeted_non_parent_prefix = {
        "activity.waitlist_reminder",
        "activity.waitlist_final_reminder",
        "activity.waitlist_expired",
        "growth_report.published",
    }
    employee_events = [
        e
        for e in NOTIFICATION_EVENT_TYPES
        if not e.startswith("parent.")
        and e != "dismissal.created"
        and e not in parent_targeted_non_parent_prefix
    ]
    for ev in employee_events:
        assert CHANNEL_MATRIX[ev] == (
            "in_app",
            "line",
        ), f"{ev} 應 ('in_app', 'line') 實 {CHANNEL_MATRIX[ev]}"


def test_parent_targeted_non_parent_prefix_events_are_line_only():
    """推家長但沒 parent. 前綴的 event（PR-C 新增）只走 LINE。"""
    line_only = [
        "activity.waitlist_reminder",
        "activity.waitlist_final_reminder",
        "activity.waitlist_expired",
        "growth_report.published",
    ]
    for ev in line_only:
        assert CHANNEL_MATRIX[ev] == (
            "line",
        ), f"{ev} 應 ('line',) 實 {CHANNEL_MATRIX[ev]}"


def test_dismissal_created_is_line_and_ws_no_in_app():
    """群組推播：LINE + 班級 WS，不寫個人 in_app。"""
    assert CHANNEL_MATRIX["dismissal.created"] == ("line", "ws")


def test_parent_events_default_line_only_except_realtime_ones():
    """家長預設 LINE only，message_received + contact_book_published 加 WS。"""
    assert CHANNEL_MATRIX["parent.message_received"] == ("line", "ws")
    assert CHANNEL_MATRIX["parent.contact_book_published"] == ("line", "ws")
    line_only = [
        "parent.announcement",
        "parent.event_ack_required",
        "parent.fee_due",
        "parent.leave_result",
        "parent.attendance_alert",
    ]
    for ev in line_only:
        assert CHANNEL_MATRIX[ev] == (
            "line",
        ), f"{ev} 應 ('line',) 實 {CHANNEL_MATRIX[ev]}"


def test_channel_type_literal():
    """Channel 應為 Literal 三值。"""
    import typing

    args = typing.get_args(Channel)
    assert set(args) == {"in_app", "line", "ws"}
