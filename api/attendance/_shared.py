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
