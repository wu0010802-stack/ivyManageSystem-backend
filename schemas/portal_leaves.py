"""教師端請假 (api/portal/leaves.py) 對應 Out schemas。

Phase 3 範圍（本檔）：
- POST /portal/my-leaves → MutationResultOut
- POST /portal/my-leaves/{id}/attachments → AttachmentUploadResultOut
- DELETE /portal/my-leaves/{id}/attachments/{filename} → DeleteResultOut
- POST /portal/my-leaves/{id}/substitute-respond → SubstituteRespondOut
- GET /portal/my-leave-stats → MyLeaveStatsOut
- GET /portal/substitute-pending-count → SubstitutePendingCountOut

Out of scope (Phase 3.5)：
- GET /portal/my-leaves (請假 list 含 substitute / attachments / approval_log)
- GET /portal/my-workday-hours (工作日時數複雜計算)
- GET /portal/my-quotas (各類假別 quota 細表)
- GET /portal/my-substitute-requests (代理請求 list)
- GET /portal/my-leaves/{id}/attachments/{filename} (FileResponse / Redirect)
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
