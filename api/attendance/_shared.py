"""
Attendance shared imports, Pydantic models, and constants.
"""

import logging
from typing import List, Optional

from pydantic import BaseModel

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


# Leave type labels (needed for calendar endpoint)
LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
}
