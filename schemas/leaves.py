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


# Backward-compat re-export — moved to schemas._common for cross-router reuse.
from schemas._common import (
    ImportFailureItem as LeaveImportFailureItem,
)  # noqa: E402,F401


class LeaveImportResultOut(IvyBaseModel):
    """POST /leaves/import Excel 批次匯入回傳（total/created/failed:int/errors）。

    與 _common.ImportResultOut ({succeeded, failed:list}) 不同：本 endpoint 回
    total/created/failed:int/errors:list[str]（與 ShiftImportResultOut 同模式，
    saved→created），前端 LeaveView 依此 shape 取用。原別名 = _common.ImportResultOut
    基於「leaves import 也回 {succeeded,failed}」的錯誤假設，與實際 return 不符
    （導致 ResponseValidationError → 端點 500）。
    """

    total: int
    created: int
    failed: int
    errors: list[str]
