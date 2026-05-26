"""event_type → 預設通道對映（宣告式 dict）。

規則：
- 'in_app' 不檢查 preference，一律寫 notification_logs；in_app 路徑由
  dispatch._fan_out 內聯實作，落 log 後自動 push inbox WS
- 'line' / 'ws' 過 notification_preferences gate（缺 row = enabled）
- 'ws' channel 只處理非 inbox WS（parent.* / dismissal.created）；員工 inbox WS
  由 _fan_out 直接呼叫 _inbox_ws_push，不經 ws adapter
- 順序即 fan-out 順序，但實作上 in_app 強制最先（log_id 是 line/ws 前置依賴）

新增 event_type 時必須在此 matrix 加一筆，否則 dispatch._fan_out 不會發。
"""

from __future__ import annotations

from typing import Literal

Channel = Literal["in_app", "line", "ws"]

CHANNEL_MATRIX: dict[str, tuple[Channel, ...]] = {
    "leave.submitted": ("in_app", "line"),
    "leave.approved": ("in_app", "line"),
    "leave.rejected": ("in_app", "line"),
    "overtime.submitted": ("in_app", "line"),
    "overtime.approved": ("in_app", "line"),
    "overtime.rejected": ("in_app", "line"),
    "punch_correction.approved": ("in_app", "line"),
    "punch_correction.rejected": ("in_app", "line"),
    "salary.batch_completed": ("in_app", "line"),
    "activity.waitlist_promoted": ("in_app", "line"),
    "pos.unlock_requested": ("in_app", "line"),
    "dismissal.created": ("line", "ws"),
    "parent.message_received": ("line", "ws"),
    "parent.announcement": ("line",),
    "parent.event_ack_required": ("line",),
    "parent.fee_due": ("line",),
    "parent.leave_result": ("line",),
    "parent.attendance_alert": ("line",),
    "parent.contact_book_published": ("line", "ws"),
    # 才藝候補家長提醒（PR-C 新增；推給家長 LINE。家長域慣例不寫 in_app）
    "activity.waitlist_reminder": ("line",),
    "activity.waitlist_final_reminder": ("line",),
    "activity.waitlist_expired": ("line",),
    # 家長 Growth Report（PR-C 新增）
    "growth_report.published": ("line",),
}
