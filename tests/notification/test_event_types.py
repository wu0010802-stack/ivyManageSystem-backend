"""event_type 常數與集合的契約測試。"""

import pytest
from services.notification.event_types import NOTIFICATION_EVENT_TYPES


def test_event_types_contains_all_19_v1_events():
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
    }
    assert NOTIFICATION_EVENT_TYPES == expected


def test_event_types_is_frozenset():
    assert isinstance(NOTIFICATION_EVENT_TYPES, frozenset)


def test_event_types_count_is_19():
    # 員工 12 + 家長 7 = 19
    assert len(NOTIFICATION_EVENT_TYPES) == 19
