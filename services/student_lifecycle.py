"""學生生命週期狀態機服務。

所有學生狀態轉移（入學、休學、復學、退學、轉出、畢業）都必須走
`transition()`，確保：
1. 非法狀態轉移被拒絕（回 ValueError，由 API 轉為 HTTP 400）
2. 每次轉移都會寫入 `StudentChangeLog`（稽核軌跡）
3. 終態不可再轉（graduated / transferred 為終態；withdrawn 可復學回 active）

禁止直接 `UPDATE students SET lifecycle_status = ...`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_PROSPECT,
    LIFECYCLE_STATUSES,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Student,
)
from models.student_log import LIFECYCLE_TO_EVENT_TYPE, StudentChangeLog
from utils.academic import resolve_current_academic_term


# 合法狀態轉移表：{from_status: {to_status: event_key}}
# event_key 對應 LIFECYCLE_TO_EVENT_TYPE（中文 event_type）
ALLOWED_TRANSITIONS: dict[str, dict[str, str]] = {
    LIFECYCLE_PROSPECT: {
        LIFECYCLE_ENROLLED: "prospect_converted",
        LIFECYCLE_WITHDRAWN: "withdrawn",
    },
    LIFECYCLE_ENROLLED: {
        LIFECYCLE_ACTIVE: "activated",
        LIFECYCLE_WITHDRAWN: "withdrawn",
    },
    LIFECYCLE_ACTIVE: {
        LIFECYCLE_ON_LEAVE: "on_leave",
        LIFECYCLE_TRANSFERRED: "transferred",
        LIFECYCLE_WITHDRAWN: "withdrawn",
        LIFECYCLE_GRADUATED: "graduated",
    },
    LIFECYCLE_ON_LEAVE: {
        LIFECYCLE_ACTIVE: "returned",
        LIFECYCLE_WITHDRAWN: "withdrawn",
    },
    # withdrawn 可復學回 active（視為「復學」）
    LIFECYCLE_WITHDRAWN: {
        LIFECYCLE_ACTIVE: "returned",
    },
    # 終態
    LIFECYCLE_TRANSFERRED: {},
    LIFECYCLE_GRADUATED: {},
}


@dataclass
class LifecycleTransitionResult:
    student_id: int
    from_status: str
    to_status: str
    event_type: str
    change_log_id: int


class LifecycleTransitionError(ValueError):
    """非法狀態轉移或輸入不合法。"""


def _validate_status(value: str, field: str) -> str:
    if value not in LIFECYCLE_STATUSES:
        raise LifecycleTransitionError(
            f"{field} 非合法生命週期狀態：{value!r}（允許：{LIFECYCLE_STATUSES}）"
        )
    return value


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    """純函式：是否允許 from_status → to_status。"""
    _validate_status(from_status, "from_status")
    _validate_status(to_status, "to_status")
    if from_status == to_status:
        return False
    return to_status in ALLOWED_TRANSITIONS.get(from_status, {})


def get_event_type_for_transition(from_status: str, to_status: str) -> str:
    """回傳對應的中文 event_type（寫入 StudentChangeLog）。"""
    key = ALLOWED_TRANSITIONS.get(from_status, {}).get(to_status)
    if key is None:
        raise LifecycleTransitionError(
            f"不允許的狀態轉移：{from_status} → {to_status}"
        )
    return LIFECYCLE_TO_EVENT_TYPE[key]


def transition(
    session: Session,
    student: Student,
    to_status: str,
    effective_date: Optional[date] = None,
    reason: Optional[str] = None,
    notes: Optional[str] = None,
    recorded_by: Optional[int] = None,
) -> LifecycleTransitionResult:
    """執行狀態轉移：驗證 → 更新 student → 寫 ChangeLog。

    呼叫端負責 session.commit()。遇到非法轉移拋 LifecycleTransitionError。
    """
    _validate_status(to_status, "to_status")
    current = student.lifecycle_status or LIFECYCLE_ACTIVE

    if not is_transition_allowed(current, to_status):
        raise LifecycleTransitionError(
            f"不允許的狀態轉移：{current} → {to_status}"
        )

    event_type = get_event_type_for_transition(current, to_status)
    event_date = effective_date or date.today()

    # 更新 student 欄位（連動舊 is_active/status/graduation/withdrawal_date）
    student.lifecycle_status = to_status
    if to_status == LIFECYCLE_GRADUATED:
        student.is_active = False
        student.status = "已畢業"
        student.graduation_date = event_date
    elif to_status == LIFECYCLE_TRANSFERRED:
        student.is_active = False
        student.status = "已轉出"
        student.withdrawal_date = event_date
    elif to_status == LIFECYCLE_WITHDRAWN:
        student.is_active = False
        student.status = "已退學"
        student.withdrawal_date = event_date
    elif to_status == LIFECYCLE_ACTIVE:
        student.is_active = True
        # 復學時清除離園日期（但保留畢業紀錄，畢業不應復學）
        if current in (LIFECYCLE_WITHDRAWN, LIFECYCLE_ON_LEAVE):
            student.withdrawal_date = None
            if student.status in ("已退學", "已刪除"):
                student.status = None
    elif to_status == LIFECYCLE_ON_LEAVE:
        # 休學仍算在讀，保留 classroom_id 與 is_active
        student.is_active = True

    # 寫稽核
    school_year, semester = resolve_current_academic_term()
    change_log = StudentChangeLog(
        student_id=student.id,
        school_year=school_year,
        semester=semester,
        event_type=event_type,
        event_date=event_date,
        classroom_id=student.classroom_id,
        reason=reason,
        notes=notes,
        recorded_by=recorded_by,
    )
    session.add(change_log)
    session.flush()  # 取得 change_log.id

    return LifecycleTransitionResult(
        student_id=student.id,
        from_status=current,
        to_status=to_status,
        event_type=event_type,
        change_log_id=change_log.id,
    )
