"""Leaves router (api/leaves.py) 對應 Out schemas。

Phase 2 範圍（本檔）：
- POST /leaves → MutationResultOut (re-use from _common)
- PUT /leaves/{id} → LeaveUpdateResultOut
- DELETE /leaves/{id} → DeleteResultOut (re-use)
- PUT /leaves/{id}/approve → LeaveApproveResultOut
- POST /leaves/import → LeaveImportResultOut

Out of scope (Phase 2.5)：
- GET /leaves (巢狀 leave list 30+ fields/筆，含 substitute_employee_name / related_swap)
- POST /leaves/batch-approve (decision/succeeded_ids/failed/approval_log_ids 等 8 欄)
- GET /leaves/import-template (Excel file)
- GET /leaves/{id}/attachments/{filename} (FileResponse / Redirect)
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel


class LeaveUpdateResultOut(IvyBaseModel):
    """PUT /leaves/{id} 回傳。

    has been-approved leave 改動會觸發 reset_to_pending（前端要顯示重送審 hint）。
    """

    message: str
    reset_to_pending: Optional[bool] = None


class LeaveApproveResultOut(IvyBaseModel):
    """PUT /leaves/{id}/approve 回傳。"""

    message: str
    warning: Optional[str] = None


class LeaveImportFailureItem(IvyBaseModel):
    """import 單筆失敗紀錄（保留 any 因 caller 結構不固定）。"""

    row: Optional[int] = None
    error: str
    data: Optional[dict[str, Any]] = None


class LeaveImportResultOut(IvyBaseModel):
    """POST /leaves/import Excel 批次匯入回傳。"""

    succeeded: int
    failed: list[LeaveImportFailureItem]
