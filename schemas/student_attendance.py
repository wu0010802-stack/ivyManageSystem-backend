"""學生每日出席 (api/student_attendance.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET  /student-attendance/overview      → StudentAttendanceDailyOverviewOut
- GET  /student-attendance               → StudentAttendanceDailyOut
- POST /student-attendance/batch         → StudentAttendanceBatchSaveResultOut
- GET  /student-attendance/by-student    → StudentAttendanceByStudentOut

Defer（暫免條件）：
- GET /student-attendance/monthly  → monthly report 內含中文 key 欄位
  （出席/缺席/病假/事假/遲到/未點名 與 student_id/name 同層為 sibling field，
  Pydantic field 不可為中文 identifier；alias 路線易與前端 contract 漂移，
  維持 grandfather 暫免，與 Phase 3.5「難描述者 defer」一致）
- GET /student-attendance/export   → Excel StreamingResponse（同 Phase 3.5
  其他 export 慣例）

PII / 教師端可見欄位：
- student_name / name（學童姓名）必標 # pii-allow:（admin/教師端必看，
  router 端已有 Permission.STUDENTS_READ gate）
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# GET /student-attendance/overview → StudentAttendanceDailyOverviewOut
# ──────────────────────────────────────────────────────────────────────


class StudentAttendanceSummaryOut(IvyBaseModel):
    """單班/全園出席摘要（對應 build_attendance_summary）。"""

    total_students: int
    recorded_count: int
    on_campus_count: int
    present_count: int
    late_count: int
    absent_count: int
    leave_count: int
    sick_leave_count: int
    personal_leave_count: int
    unmarked_count: int
    record_completion_rate: float
    attendance_rate: float


class StudentAttendanceOverviewClassroomOut(IvyBaseModel):
    """總覽內單班一筆（含當班統計 + rollcall_status）。"""

    classroom_id: int
    classroom_name: str
    student_count: int
    recorded_count: int
    on_campus_count: int
    present_count: int
    late_count: int
    absent_count: int
    leave_count: int
    unmarked_count: int
    record_completion_rate: float
    attendance_rate: float
    last_recorded_at: Optional[str] = None
    last_recorded_by: Optional[str] = None
    rollcall_status: str


class StudentAttendanceDailyOverviewOut(IvyBaseModel):
    """GET /student-attendance/overview — 全園當日各班出席總覽。"""

    date: str
    totals: StudentAttendanceSummaryOut
    classrooms: list[StudentAttendanceOverviewClassroomOut]


# ──────────────────────────────────────────────────────────────────────
# GET /student-attendance → StudentAttendanceDailyOut
# ──────────────────────────────────────────────────────────────────────


class StudentAttendanceDailyRecordOut(IvyBaseModel):
    """單班當日單一學生一筆（未點名時 status/remark = None）。"""

    student_id: int
    student_no: Optional[str] = None
    name: str  # pii-allow: 學童姓名（教師端必看）
    status: Optional[str] = None
    remark: Optional[str] = None


class StudentAttendanceDailyOut(IvyBaseModel):
    """GET /student-attendance — 某班某日出席清單（含未點名）。"""

    date: str
    classroom_id: int
    records: list[StudentAttendanceDailyRecordOut]


# ──────────────────────────────────────────────────────────────────────
# POST /student-attendance/batch → StudentAttendanceBatchSaveResultOut
# ──────────────────────────────────────────────────────────────────────


class StudentAttendanceBatchSaveResultOut(IvyBaseModel):
    """POST /student-attendance/batch — 批次 upsert 結果 ({message, saved})。"""

    message: str
    saved: int


# ──────────────────────────────────────────────────────────────────────
# GET /student-attendance/by-student → StudentAttendanceByStudentOut
# ──────────────────────────────────────────────────────────────────────


class StudentAttendanceByStudentItemOut(IvyBaseModel):
    """單一學生紀錄抽屜中單筆出席紀錄。"""

    id: int
    date: Optional[str] = None
    status: Optional[str] = None
    remark: Optional[str] = None
    source_leave_id: Optional[int] = None


class StudentAttendanceByStudentOut(IvyBaseModel):
    """GET /student-attendance/by-student — 學生紀錄抽屜回傳。

    counts 採 Dict[str, int]，key 為中文出席狀態（出席/缺席/病假/事假/遲到），
    與 VALID_STATUSES tuple 對齊，避免 alias 漂移。
    """

    student_id: int
    student_name: str  # pii-allow: 學童姓名（教師端必看）
    items: list[StudentAttendanceByStudentItemOut]
    total: int
    counts: dict[str, int]
