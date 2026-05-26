"""event_type 常數與集合的契約測試。"""

from services.notification.event_types import NOTIFICATION_EVENT_TYPES


def test_event_types_contains_all_23_v1_events():
    expected = {
        # 員工域 (12)
        "leave.submitted",
        "leave.approved",
        "leave.rejected",
        "overtime.submitted",
        "overtime.approved",
        "overtime.rejected",
        "punch_correction.approved",
        "punch_correction.rejected",
        "salary.batch_completed",
        "activity.waitlist_promoted",
        "pos.unlock_requested",
        "dismissal.created",
        # 家長域 (7)
        "parent.message_received",
        "parent.announcement",
        "parent.event_ack_required",
        "parent.fee_due",
        "parent.leave_result",
        "parent.attendance_alert",
        "parent.contact_book_published",
        # 才藝家長域 (3) — PR-C 新增
        "activity.waitlist_reminder",
        "activity.waitlist_final_reminder",
        "activity.waitlist_expired",
        # 家長 Growth Report (1) — PR-C 新增
        "growth_report.published",
    }
    assert NOTIFICATION_EVENT_TYPES == expected


def test_event_types_is_frozenset():
    assert isinstance(NOTIFICATION_EVENT_TYPES, frozenset)


def test_event_types_count_is_23():
    # 員工 12 + 家長 7 + 才藝家長 3 + Growth Report 1 = 23
    assert len(NOTIFICATION_EVENT_TYPES) == 23
