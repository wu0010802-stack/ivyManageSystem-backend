"""Shifts router (api/shifts.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /types                  → list[ShiftTypeOut]
- POST   /types                  → ShiftTypeOut
- PUT    /types/{type_id}        → ShiftTypeOut
- GET    /assignments            → list[ShiftAssignmentOut]
- POST   /assignments            → ShiftAssignmentSaveResultOut
- GET    /daily                  → list[DailyShiftOut]
- POST   /daily                  → DeleteResultOut (re-use from _common)
- GET    /swap-history           → list[ShiftSwapHistoryOut]
- POST   /import                 → ShiftImportResultOut

Out of scope（已有 schema 或無法 schema 化）：
- DELETE /types/{type_id}        → DeleteResultOut (Phase 3 已落地)
- DELETE /daily/{shift_id}       → DeleteResultOut (Phase 3 已落地)
- GET    /import-template        → StreamingResponse (xlsx 檔案)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class ShiftTypeOut(IvyBaseModel):
    """班別模板單筆 (GET /types list / POST /types / PUT /types/{id})。"""

    id: int
    name: str
    work_start: str
    work_end: str
    sort_order: int
    is_active: bool


class ShiftAssignmentOut(IvyBaseModel):
    """每週排班單筆 (GET /assignments)。"""

    id: int
    employee_id: int
    employee_name: str
    shift_type_id: Optional[int] = None
    shift_type_name: str
    work_start: str
    work_end: str
    week_start_date: str
    notes: str


class WeeklyHoursWarningOut(IvyBaseModel):
    """週工時超時預警單筆 (POST /assignments warnings 子項)。

    對齊 utils/schedule_utils.build_weekly_warning 回傳 shape。
    """

    code: str
    employee_id: int
    employee_name: str
    week_start: str
    calculated_hours: float
    limit_hours: float
    message: str


class ShiftAssignmentSaveResultOut(IvyBaseModel):
    """POST /assignments 批次儲存回傳。

    warnings 僅在有員工超時才出現；無超時則欄位省略（router 端條件 set）。
    """

    message: str
    week_start_date: str
    warnings: Optional[list[WeeklyHoursWarningOut]] = None


class DailyShiftOut(IvyBaseModel):
    """每日排班單筆 (GET /daily)。"""

    id: int
    employee_id: int
    employee_name: str
    # 排休日合法為 NULL（models/shift.py：shift_type_id nullable）；
    # 非 Optional 會使含排休日的 GET /shifts/daily 整批 ResponseValidationError → 500。
    shift_type_id: Optional[int] = None
    shift_type_name: str
    work_start: str
    work_end: str
    date: str
    notes: str


class ShiftSwapHistoryOut(IvyBaseModel):
    """換班歷史單筆 (GET /swap-history)。"""

    id: int
    requester_name: str
    target_name: str
    swap_date: str
    requester_shift: str
    target_shift: str
    reason: Optional[str] = None
    status: str
    target_remark: Optional[str] = None
    target_responded_at: Optional[str] = None
    created_at: Optional[str] = None


class ShiftImportResultOut(IvyBaseModel):
    """POST /import Excel 批次匯入回傳。

    Note: shifts import 用 total/saved/failed:int/errors:list[str] shape，
    與 overtimes import (total/created/failed/errors) 命名不同；不可共用。
    """

    total: int
    saved: int
    failed: int
    errors: list[str]
