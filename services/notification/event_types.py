"""通知 event_type 命名空間：兩級 {domain}.{action}。

v1 共 19 個 event（員工 12 + 家長 7）。新增 event 時：
1. 加進此 frozenset
2. 在 channel_matrix.py 加對應 channel tuple
3. 在 renderers.py 加 @renderer 裝飾的函式
4. （家長端）若家長可關 → notification_preferences row 由 caller 控
"""

from __future__ import annotations

# 員工域
LEAVE_SUBMITTED = "leave.submitted"
LEAVE_APPROVED = "leave.approved"
LEAVE_REJECTED = "leave.rejected"
OVERTIME_SUBMITTED = "overtime.submitted"
OVERTIME_APPROVED = "overtime.approved"
OVERTIME_REJECTED = "overtime.rejected"
PUNCH_CORRECTION_APPROVED = "punch_correction.approved"
PUNCH_CORRECTION_REJECTED = "punch_correction.rejected"
SALARY_BATCH_COMPLETED = "salary.batch_completed"
ACTIVITY_WAITLIST_PROMOTED = "activity.waitlist_promoted"
POS_UNLOCK_REQUESTED = "pos.unlock_requested"
DISMISSAL_CREATED = "dismissal.created"

# 家長域
PARENT_MESSAGE_RECEIVED = "parent.message_received"
PARENT_ANNOUNCEMENT = "parent.announcement"
PARENT_EVENT_ACK_REQUIRED = "parent.event_ack_required"
PARENT_FEE_DUE = "parent.fee_due"
PARENT_LEAVE_RESULT = "parent.leave_result"
PARENT_ATTENDANCE_ALERT = "parent.attendance_alert"
PARENT_CONTACT_BOOK_PUBLISHED = "parent.contact_book_published"

NOTIFICATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        LEAVE_SUBMITTED,
        LEAVE_APPROVED,
        LEAVE_REJECTED,
        OVERTIME_SUBMITTED,
        OVERTIME_APPROVED,
        OVERTIME_REJECTED,
        PUNCH_CORRECTION_APPROVED,
        PUNCH_CORRECTION_REJECTED,
        SALARY_BATCH_COMPLETED,
        ACTIVITY_WAITLIST_PROMOTED,
        POS_UNLOCK_REQUESTED,
        DISMISSAL_CREATED,
        PARENT_MESSAGE_RECEIVED,
        PARENT_ANNOUNCEMENT,
        PARENT_EVENT_ACK_REQUIRED,
        PARENT_FEE_DUE,
        PARENT_LEAVE_RESULT,
        PARENT_ATTENDANCE_ALERT,
        PARENT_CONTACT_BOOK_PUBLISHED,
    }
)
