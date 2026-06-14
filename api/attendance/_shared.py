"""
Attendance shared imports, Pydantic models, and constants.
"""

import logging
from typing import List, Optional

from pydantic import BaseModel, Field
from utils.constants import LEAVE_TYPE_LABELS
from utils.excel_io import MAX_IMPORT_ROWS

logger = logging.getLogger(__name__)


# ============ Pydantic Models ============


class AttendanceCSVRow(BaseModel):
    """CSV 考勤記錄格式"""

    department: str
    employee_number: str
    name: str
    date: str
    weekday: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


class AttendanceUploadRequest(BaseModel):
    """CSV 考勤上傳請求"""

    # C4：與 xlsx 路徑（utils.excel_io.MAX_IMPORT_ROWS）對齊的列數上限，防認證後
    # 送入超大 records 撐爆記憶體（OOM DoS）。Pydantic 解析階段即拒，先於 handler。
    records: List[AttendanceCSVRow] = Field(..., max_length=MAX_IMPORT_ROWS)
    year: int
    month: int


class AttendanceRecordUpdate(BaseModel):
    """單筆考勤記錄更新"""

    employee_id: int
    date: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


# ============ Pure helpers ============


def compute_shift_based_attendance(
    dt_in_full, dt_out_full, shift_start_dt, shift_end_dt
):
    """以排班基準計算 (is_late, is_early_leave, late_minutes, early_leave_minutes, status)。

    C15：排班教師的 is_late/is_early_leave 走排班 shift 基準，遲到/早退分鐘也必須同基準
    （而非 parser 預設 08:00/17:00 基準），否則旗標與扣款分鐘脫鉤造成少扣/多扣。
    旗標與分鐘在此單一函式同源計算，供 legacy 匯入路徑回填 detail。

    參數皆為 datetime（跨夜班的 shift_end_dt / punch_out 已是次日）。
    """
    is_late = dt_in_full > shift_start_dt
    late_minutes = (
        max(0, int((dt_in_full - shift_start_dt).total_seconds() / 60))
        if is_late
        else 0
    )
    is_early_leave = dt_out_full < shift_end_dt
    early_leave_minutes = (
        max(0, int((shift_end_dt - dt_out_full).total_seconds() / 60))
        if is_early_leave
        else 0
    )
    if is_late and is_early_leave:
        status = "late+early_leave"
    elif is_late:
        status = "late"
    elif is_early_leave:
        status = "early_leave"
    else:
        status = "normal"
    return is_late, is_early_leave, late_minutes, early_leave_minutes, status
