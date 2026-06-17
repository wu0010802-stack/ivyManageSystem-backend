"""考勤匯入預覽 Pydantic schema。

AttendancePreviewRequest  → POST /api/attendance/upload/preview 請求體
AttendancePreviewResult   → 回應：summary + 逐列檢核 + normalized 列表
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from api.attendance._shared import MAX_IMPORT_ROWS, AttendanceCSVRow


class AttendancePreviewRequest(BaseModel):
    raw_text: Optional[str] = None
    records: Optional[List[AttendanceCSVRow]] = Field(
        default=None, max_length=MAX_IMPORT_ROWS
    )


class PreviewRow(BaseModel):
    row_num: int
    employee_number: str
    employee_name: str
    matched_employee_id: Optional[int] = None
    date: Optional[str] = None
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None
    status: Optional[str] = None
    check: Literal[
        "importable",
        "employee_not_found",
        "invalid_date",
        "month_finalized",
        "overwrite",
    ]


class PreviewSummary(BaseModel):
    importable: int
    problems: int
    overwrites: int


class AttendancePreviewResult(BaseModel):
    summary: PreviewSummary
    rows: List[PreviewRow]
    normalized: List[AttendanceCSVRow]
