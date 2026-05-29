"""考勤紀錄 CRUD (api/attendance/records.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /records                              → AttendanceRecordListOut（list[AttendanceRecordItemOut]）
- POST   /record                               → AttendanceRecordUpsertResultOut
- DELETE /record/{employee_id}/{date}          → DeleteResultOut（共用）
- DELETE /records/{employee_id}/{date_str}     → DeleteResultOut（共用）
- DELETE /records/{year}/{month}               → DeleteResultOut（共用）

Defer（暫免條件）：
- 無；本檔 5 條皆落地。

PII / 教師端可見欄位：
- employee_name 不在 _PII_KEY_SUBSTRINGS 內（denylist 為 student_name /
  parent_name / child_name），故不需 pii-allow；但「員工姓名」對 admin/HR
  仍是合理敏感資料，已由 Permission.ATTENDANCE_READ gate 控制。
- punch_in / punch_out 為時間字串（HH:MM）非 timestamp 無 IP 帶入，無需 pii-allow。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# GET /records → AttendanceRecordListOut
# ──────────────────────────────────────────────────────────────────────


class AttendanceRecordItemOut(IvyBaseModel):
    """單筆考勤紀錄（含員工姓名/工號摘要 + 上下班/狀態/缺打卡旗標）。

    對應 router 內 result.append({...}) 的欄位順序與 shape。
    """

    id: int
    employee_id: int
    employee_name: str
    employee_number: str
    date: str
    weekday: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None
    status: Optional[str] = None
    is_late: Optional[bool] = None
    is_early_leave: Optional[bool] = None
    is_missing_punch_in: Optional[bool] = None
    is_missing_punch_out: Optional[bool] = None
    late_minutes: Optional[int] = None
    early_leave_minutes: Optional[int] = None
    remark: Optional[str] = None


# router 直接 return list[...]，FastAPI response_model 可用 list[Item] 表達；
# 但 OpenAPI codegen 友善起見另提供 List alias type；router 端用 list[Item]。
AttendanceRecordListOut = list[AttendanceRecordItemOut]


# ──────────────────────────────────────────────────────────────────────
# POST /record → AttendanceRecordUpsertResultOut
# ──────────────────────────────────────────────────────────────────────


class AttendanceRecordUpsertResultOut(IvyBaseModel):
    """POST /record — 新增或更新單筆考勤後回傳重算結果。

    對應 router 末段 return {message, status, is_late, late_minutes,
    is_early_leave, early_leave_minutes}。
    """

    message: str
    status: Optional[str] = None
    is_late: Optional[bool] = None
    late_minutes: Optional[int] = None
    is_early_leave: Optional[bool] = None
    early_leave_minutes: Optional[int] = None
