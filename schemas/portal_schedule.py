"""教師端課表/換班 (api/portal/schedule.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET  /portal/my-schedule                          → ScheduleMyScheduleOut
- GET  /portal/swap-candidates                      → list[ScheduleSwapCandidateOut]
- GET  /portal/swap-requests                        → list[ScheduleSwapRequestOut]
- POST /portal/swap-requests                        → ScheduleSwapCreateOut
- POST /portal/swap-requests/{id}/respond           → ScheduleSwapRespondOut
- POST /portal/swap-requests/{id}/cancel            → DeleteResultOut (純 message)
- GET  /portal/swap-pending-count                   → ScheduleSwapPendingCountOut

教師姓名（employee_name / requester_name / target_name 等）統一標 pii-allow：
教師端本就需顯示同事姓名以進行排班/換班操作（對齊 portal_students /
portal_contact_book 慣例）。

週工時超時 warning shape 來自 utils/schedule_utils.check_weekly_hours_warning，
employee_name 同樣標 pii-allow。

cached 早退（FastAPIResponse 304）情境：FastAPI 允許 path function 直接回傳
Response，會 bypass response_model 驗證，因此 GET /my-schedule 雖標
ScheduleMyScheduleOut 亦不影響快取早退路徑。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# 共用 warning（換班影響週工時時附加）
# ──────────────────────────────────────────────────────────────────────


class ScheduleWeeklyHoursWarning(IvyBaseModel):
    """週工時超時 warning（utils/schedule_utils.check_weekly_hours_warning）。"""

    code: str
    employee_id: int
    employee_name: str  # pii-allow: 教師姓名（教師端排班 UI 必看）
    week_start: str
    calculated_hours: float
    limit_hours: float
    message: str


# ──────────────────────────────────────────────────────────────────────
# GET /my-schedule → ScheduleMyScheduleOut
# ──────────────────────────────────────────────────────────────────────


class ScheduleDayItem(IvyBaseModel):
    """my-schedule 內某一日的排班資訊。"""

    date: str
    day: int
    weekday: str
    is_weekend: bool
    is_makeup_workday: bool
    shift_type_id: Optional[int] = None
    shift_name: Optional[str] = None
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    is_override: bool


class ScheduleMyScheduleOut(IvyBaseModel):
    """GET /portal/my-schedule — 教師當月排班。"""

    employee_name: str  # pii-allow: 教師姓名（自身可見）
    year: int
    month: int
    days: list[ScheduleDayItem]


# ──────────────────────────────────────────────────────────────────────
# GET /swap-candidates → list[ScheduleSwapCandidateOut]
# ──────────────────────────────────────────────────────────────────────


class ScheduleSwapCandidateOut(IvyBaseModel):
    """GET /portal/swap-candidates list 單筆（其他老師當日班別）。"""

    employee_id: int
    name: str  # pii-allow: 教師姓名（教師端換班 UI 必看）
    shift_type_id: Optional[int] = None
    shift_name: str
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    has_pending_swap: bool


# ──────────────────────────────────────────────────────────────────────
# GET /swap-requests → list[ScheduleSwapRequestOut]
# ──────────────────────────────────────────────────────────────────────


class ScheduleSwapRequestOut(IvyBaseModel):
    """GET /portal/swap-requests list 單筆（自己發起 + 收到的換班申請）。"""

    id: int
    requester_id: int
    requester_name: str  # pii-allow: 教師姓名（教師端換班 UI 必看）
    target_id: int
    target_name: str  # pii-allow: 教師姓名（教師端換班 UI 必看）
    swap_date: str
    requester_shift: str
    target_shift: str
    reason: Optional[str] = None
    status: str
    target_remark: Optional[str] = None
    target_responded_at: Optional[str] = None
    created_at: Optional[str] = None
    is_mine: bool


# ──────────────────────────────────────────────────────────────────────
# POST /swap-requests → ScheduleSwapCreateOut
# ──────────────────────────────────────────────────────────────────────


class ScheduleSwapCreateOut(IvyBaseModel):
    """POST /portal/swap-requests — 發起換班申請。

    warnings 為週工時超時非阻斷預警（雙方任一週工時超勞基法上限即附加）。
    """

    message: str
    id: int
    warnings: Optional[list[ScheduleWeeklyHoursWarning]] = None


# ──────────────────────────────────────────────────────────────────────
# POST /swap-requests/{id}/respond → ScheduleSwapRespondOut
# ──────────────────────────────────────────────────────────────────────


class ScheduleSwapRespondOut(IvyBaseModel):
    """POST /portal/swap-requests/{id}/respond — 接受/拒絕回傳。

    接受時 warnings 為週工時超時非阻斷預警；拒絕時無 warnings。
    """

    message: str
    warnings: Optional[list[ScheduleWeeklyHoursWarning]] = None


# ──────────────────────────────────────────────────────────────────────
# GET /swap-pending-count → ScheduleSwapPendingCountOut
# ──────────────────────────────────────────────────────────────────────


class ScheduleSwapPendingCountOut(IvyBaseModel):
    """GET /portal/swap-pending-count — 待回覆換班數（badge 用）。"""

    pending_count: int
