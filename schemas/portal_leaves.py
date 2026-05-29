"""教師端請假 (api/portal/leaves.py) 對應 Out schemas。

Phase 3 範圍（已 ship）：
- POST /portal/my-leaves → MutationResultOut
- POST /portal/my-leaves/{id}/attachments → AttachmentUploadResultOut
- DELETE /portal/my-leaves/{id}/attachments/{filename} → DeleteResultOut
- POST /portal/my-leaves/{id}/substitute-respond → SubstituteRespondOut
- GET /portal/my-leave-stats → MyLeaveStatsOut
- GET /portal/substitute-pending-count → SubstitutePendingCountOut

Phase 3.5 範圍（本檔追加）：
- GET /portal/my-leaves → list[MyLeaveListItemOut]
- GET /portal/my-workday-hours → MyWorkdayHoursOut（含 breakdown 子項）
- GET /portal/my-quotas → list[MyQuotaItemOut]
- GET /portal/my-substitute-requests → list[MySubstituteRequestItemOut]

Out of scope (defer)：
- GET /portal/my-leaves/{id}/attachments/{filename} (Response / RedirectResponse
  動態回傳 — local backend 走 bytes Response，supabase backend 走 302 Redirect，
  非 JSON 不適合 response_model)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class AttachmentUploadResultOut(IvyBaseModel):
    """POST /portal/my-leaves/{id}/attachments — 上傳附件回傳。"""

    message: str
    attachments: list[str]  # 含 storage path / url


class SubstituteRespondOut(IvyBaseModel):
    """POST /portal/my-leaves/{id}/substitute-respond — 代理回應。"""

    message: str


class SubstitutePendingCountOut(IvyBaseModel):
    """GET /portal/substitute-pending-count — 教師端代理待回應計數。"""

    pending_count: int


class MyLeaveStatsOut(IvyBaseModel):
    """GET /portal/my-leave-stats — 教師本人特休統計。"""

    hire_date: Optional[str] = None
    seniority_years: int
    seniority_months: int
    annual_leave_quota: float
    annual_leave_used_days: float
    start_of_calculation: str
    end_of_calculation: str


# ──────────────────────────────────────────────────────────────────────
# GET /portal/my-leaves — 個人請假列表（指定年月）
# ──────────────────────────────────────────────────────────────────────


class MyLeaveListItemOut(IvyBaseModel):
    """GET /portal/my-leaves items 單筆假單。

    `reason` 為員工自填假單理由（本人查看自己 — 不視為跨人 PII 但內容敏感）；
    `substitute_remark` 同理。對齊 Sentry denylist exempt 機制。
    """

    id: int
    leave_type: str
    leave_type_label: str
    start_date: str
    end_date: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: float
    reason: Optional[str] = None  # pii-allow: 本人查看自填假單理由
    status: str
    approved_by: Optional[int] = None
    rejection_reason: Optional[str] = None
    attachment_paths: list[str]
    substitute_employee_id: Optional[int] = None
    substitute_status: str
    substitute_remark: Optional[str] = None  # pii-allow: 代理人回覆備註
    source_overtime_id: Optional[int] = None
    created_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET /portal/my-workday-hours — 區間每日工時明細
# ──────────────────────────────────────────────────────────────────────


class MyWorkdayHoursBreakdownItem(IvyBaseModel):
    """breakdown 單日項目 — weekend / holiday / workday 三 branch 共用同 11 欄
    shape（_build_workday_hours_payload）。`hours` 在 weekend/holiday branch
    為 0 (int)、workday branch 為 float — Pydantic float 自動 coerce。"""

    date: str
    weekday: int
    type: str  # "weekend" / "holiday" / "workday"
    hours: float
    shift: Optional[str] = None
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    holiday_name: Optional[str] = None
    is_makeup_workday: bool
    workday_override_name: Optional[str] = None
    source: Optional[str] = None  # "daily" / "weekly" / "default" / None


class MyWorkdayHoursOut(IvyBaseModel):
    """GET /portal/my-workday-hours 回傳 — total_hours + breakdown。"""

    total_hours: float
    breakdown: list[MyWorkdayHoursBreakdownItem]


# ──────────────────────────────────────────────────────────────────────
# GET /portal/my-quotas — 個人各假別年度配額（含 used / pending / remaining）
# ──────────────────────────────────────────────────────────────────────


class MyQuotaItemOut(IvyBaseModel):
    """GET /portal/my-quotas 單筆假別配額。"""

    leave_type: str
    leave_type_label: str
    total_hours: float
    used_hours: float
    pending_hours: float
    remaining_hours: float
    note: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET /portal/my-substitute-requests — 被指定為代理人的假單列表
# ──────────────────────────────────────────────────────────────────────


class MySubstituteRequestItemOut(IvyBaseModel):
    """GET /portal/my-substitute-requests 單筆代理請求。

    `requester_name` / `requester_employee_id` 為申請人資訊（同事姓名/工號），
    教師端必看；`reason` 為申請人自填理由，`substitute_remark` 為本人回覆備註。"""

    id: int
    leave_type: str
    leave_type_label: str
    requester_name: str  # pii-allow: 申請人姓名（代理人必看）
    requester_employee_id: str  # pii-allow: 申請人工號（代理人必看）
    start_date: str
    end_date: str
    leave_hours: float
    reason: Optional[str] = None  # pii-allow: 申請人自填假單理由
    substitute_status: str
    substitute_responded_at: Optional[str] = None
    status: str
    created_at: Optional[str] = None
