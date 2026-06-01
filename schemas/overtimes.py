"""Overtimes router (api/overtimes.py) 對應 Out schemas。

Phase 2 範圍（本檔）：
- POST /overtimes → OvertimeCreateResultOut
- PUT /overtimes/{id} → OvertimeUpdateResultOut
- DELETE /overtimes/{id} → OvertimeDeleteResultOut
- PUT /overtimes/{id}/approve → OvertimeApproveResultOut
- POST /overtimes/import → ImportResultOut (re-use from _common)

Out of scope (Phase 2.5)：
- GET /overtimes (巢狀 list)
- POST /overtimes/batch-approve (decision/succeeded_ids/failed/audit_log 等複雜結果)
- GET /overtimes/import-template (Excel file response)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class OvertimeCreateResultOut(IvyBaseModel):
    """POST /overtimes 回傳 — 新增成功 + 計算出的加班費。"""

    message: str
    id: int
    overtime_pay: Optional[float] = None  # pii-allow: 加班費（薪資金額）


class OvertimeUpdateResultOut(IvyBaseModel):
    """PUT /overtimes/{id} 回傳。

    若改到已核准的單，會自動退回「待審核」（reset_to_pending=True），
    並 trigger 薪資重算（salary_recalculated + 可能的 warning）。
    """

    message: str
    overtime_pay: Optional[float] = None  # pii-allow: 加班費（薪資金額）
    reset_to_pending: Optional[bool] = None
    salary_recalculated: Optional[bool] = None  # pii-allow: 觸發旗標，非個人薪資金額
    warning: Optional[str] = None


class OvertimeDeleteResultOut(IvyBaseModel):
    """DELETE /overtimes/{id} 回傳。

    刪除已核准單 trigger 薪資重算 → salary_recalculated + 可能 warning。
    """

    message: str
    salary_recalculated: Optional[bool] = None  # pii-allow: 觸發旗標，非個人薪資金額
    warning: Optional[str] = None


class OvertimeImportResultOut(IvyBaseModel):
    """POST /overtimes/import Excel 批次匯入回傳。

    Note: overtimes import 用 total/created/failed:int/errors:list[str] shape，
    與 leaves import {succeeded:int, failed:list} 不同；不可共用 _common.ImportResultOut。
    """

    total: int
    created: int
    failed: int
    errors: list[str]


class OvertimeApproveResultOut(IvyBaseModel):
    """PUT /overtimes/{id}/approve 回傳。

    核准 use_comp_leave=True 的單會發放補休配額（comp_leave_hours_granted）。
    後續薪資重算 → salary_recalculated + 可能 warning。
    """

    message: str
    comp_leave_hours_granted: Optional[float] = None
    salary_recalculated: Optional[bool] = None  # pii-allow: 觸發旗標，非個人薪資金額
    warning: Optional[str] = None


class BatchOvertimeCreateResultOut(IvyBaseModel):
    """POST /overtimes/batch-create 成功回傳（全部建立）。

    驗證失敗時回 422，body 為 {"detail": {"message": str, "errors": list}}，
    不走本 response_model（FastAPI HTTPException 路徑）。
    """

    message: str
    created_ids: list[int]
