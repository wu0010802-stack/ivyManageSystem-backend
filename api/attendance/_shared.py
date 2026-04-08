"""
Attendance shared imports, Pydantic models, and constants.
"""

import logging
from typing import List, Optional

from pydantic import BaseModel
from utils.constants import LEAVE_TYPE_LABELS

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
    records: List[AttendanceCSVRow]
    year: int
    month: int


class AttendanceRecordUpdate(BaseModel):
    """單筆考勤記錄更新"""
    employee_id: int
    date: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


