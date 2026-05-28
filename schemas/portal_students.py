"""教師端（portal）students router 對應 Out schemas。

範圍：
- StudentMeasurementSnapshotItem / RevealPhoneOut — 簡單 shape
- MyStudentsOut（含 ClassroomBlock + StudentBlockItem）— GET /my-students
- StudentDetailOut（含 10+ nested model 200+ 行）— GET /students/{id}/detail
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel


class LastMeasurement(IvyBaseModel):
    """單筆量測快照；string 值對齊 router 內 `str(Decimal)` 序列化."""

    measured_on: str
    height_cm: Optional[str] = None  # pii-allow: 學童身高（健康資料，admin/teacher 看）
    weight_kg: Optional[str] = None  # pii-allow: 學童體重
    head_circumference_cm: Optional[str] = None
    vision_left: Optional[str] = None
    vision_right: Optional[str] = None


class StudentMeasurementSnapshotItem(IvyBaseModel):
    """GET /students/measurements-latest list 單筆。"""

    student_id: int
    name: str  # pii-allow: 學童姓名（教師端必看）
    classroom_id: Optional[int] = None
    last_measurement: Optional[LastMeasurement] = None


class RevealPhoneOut(IvyBaseModel):
    """POST /students/{id}/reveal-phone 揭露電話。"""

    target: str  # "parent" / "emergency" / "guardian"
    guardian_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)
    phone: str  # pii-allow: 揭露的完整電話（高敏感事件已 audit）


# ──────────────────────────────────────────────────────────────────────
# GET /portal/my-students — 教師所屬班級的學生資料（精簡欄位 + 健康/出席聚合）
# ──────────────────────────────────────────────────────────────────────


class MyStudentsStudentItem(IvyBaseModel):
    """my-students 內單一學生欄位（精簡 + 健康/出席聚合）。"""

    id: int
    student_id: str
    name: str  # pii-allow: 學童姓名（教師端必看）
    gender: Optional[str] = None
    birthday: Optional[str] = None  # pii-allow: 生日 ISO（教師端看）
    enrollment_date: Optional[str] = None
    lifecycle_status: Optional[str] = None
    parent_name: Optional[str] = None  # pii-allow: 監護人名（教師端看）
    parent_phone_masked: Optional[str] = (
        None  # pii-allow: masked phone（明碼走 reveal）
    )
    status_tag: Optional[str] = None
    notes: Optional[str] = None
    has_health_alert: bool = False
    health_alert_count: int = 0
    attendance_rate_this_month: Optional[float] = None
    last_absent_date: Optional[str] = None


class MyStudentsClassroomBlock(IvyBaseModel):
    """my-students 一個班級 + 學生 list。"""

    classroom_id: int
    classroom_name: str
    role: str  # "head" / "assistant" / "art" / "viewer"
    student_count: int
    students: list[MyStudentsStudentItem]


class MyStudentsOut(IvyBaseModel):
    """GET /my-students 教師班級聚合 response。"""

    employee_name: str  # pii-allow: 教師姓名（自身可見）
    classrooms: list[MyStudentsClassroomBlock]
    total_students: int


# ──────────────────────────────────────────────────────────────────────
# GET /portal/students/{id}/detail — 單一學生彙總頁
# ──────────────────────────────────────────────────────────────────────


class StudentDetailStudent(IvyBaseModel):
    """detail 內 student 基本資料區（紅線：不回 address）。"""

    id: int
    student_id: str
    name: str  # pii-allow: 學童姓名
    gender: Optional[str] = None
    birthday: Optional[str] = None  # pii-allow: 生日 ISO
    enrollment_date: Optional[str] = None
    lifecycle_status: Optional[str] = None
    status_tag: Optional[str] = None
    notes: Optional[str] = None
    allergy_text: Optional[str] = None  # deprecated; per STUDENTS_HEALTH_READ
    medication_text: Optional[str] = None  # deprecated; per STUDENTS_HEALTH_READ
    special_needs: Optional[str] = None  # per STUDENTS_SPECIAL_NEEDS_READ
    emergency_contact_name: Optional[str] = None  # pii-allow: 緊急聯絡人
    emergency_contact_phone_masked: Optional[str] = None  # pii-allow: masked
    emergency_contact_relation: Optional[str] = None
    parent_name: Optional[str] = None  # pii-allow: 監護人名
    parent_phone_masked: Optional[str] = None  # pii-allow: masked


class StudentDetailClassroom(IvyBaseModel):
    id: int
    name: str
    viewer_role: Optional[str] = None  # admin 時為 None


class StudentDetailGuardian(IvyBaseModel):
    id: int
    name: str  # pii-allow: 監護人名
    phone_masked: Optional[str] = None  # pii-allow: masked
    email: Optional[str] = None  # pii-allow: 監護人 email
    relation: Optional[str] = None
    is_primary: bool
    is_emergency: bool
    can_pickup: bool
    user_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)


class StudentDetailAllergy(IvyBaseModel):
    id: int
    allergen: str
    severity: Optional[str] = None
    reaction: Optional[str] = None
    first_aid_note: Optional[str] = None


class StudentDetailMedicationOrder(IvyBaseModel):
    id: int
    order_date: str
    medication_name: str
    dose: Optional[str] = None
    time_slots: Any = None  # ARRAY[str] 或 JSON list；以 router 原樣回傳
    source: Optional[str] = None
    note: Optional[str] = None


class StudentDetailHealth(IvyBaseModel):
    allergies: list[StudentDetailAllergy]
    recent_medication_orders: list[StudentDetailMedicationOrder]


class StudentDetailAttendanceSummary(IvyBaseModel):
    present: int = 0
    absent: int = 0
    late: int = 0
    leave: int = 0


class StudentDetailAttendanceDay(IvyBaseModel):
    date: str
    status: str
    remark: Optional[str] = None


class StudentDetailAttendance30d(IvyBaseModel):
    summary: StudentDetailAttendanceSummary
    by_day: list[StudentDetailAttendanceDay]


class StudentDetailAttendanceThisMonth(IvyBaseModel):
    rate: Optional[float] = None
    last_absent_date: Optional[str] = None


class StudentDetailIncident(IvyBaseModel):
    id: int
    incident_date: Optional[str] = None
    type: Optional[str] = None
    severity: Optional[str] = None
    description: Optional[str] = None


class StudentDetailObservation(IvyBaseModel):
    id: int
    observation_date: Optional[str] = None
    domain: Optional[str] = None
    narrative: Optional[str] = None
    rating: Optional[str] = None
    is_highlight: bool = False


class StudentDetailAssessment(IvyBaseModel):
    id: int
    semester: Optional[str] = None
    assessment_type: Optional[str] = None
    domain: Optional[str] = None
    rating: Optional[str] = None
    assessment_date: Optional[str] = None


class StudentDetailContactBook(IvyBaseModel):
    id: int
    log_date: str
    published_at: Optional[str] = None
    mood: Optional[str] = None
    teacher_note: Optional[str] = None


class StudentDetailOut(IvyBaseModel):
    """GET /students/{id}/detail 學生彙總頁 response。"""

    student: StudentDetailStudent
    classroom: Optional[StudentDetailClassroom] = None
    guardians: list[StudentDetailGuardian]  # pii-allow: nested block 名（教師端必看）
    health: StudentDetailHealth  # pii-allow: nested block 名（健康資料子結構）
    attendance_30d: StudentDetailAttendance30d
    attendance_this_month: StudentDetailAttendanceThisMonth
    transfer_history: Any  # _build_transfer_history 結構由 helper 決定，未型別化
    recent_incidents_30d: list[StudentDetailIncident]
    recent_observations_30d: list[StudentDetailObservation]
    recent_assessments: list[StudentDetailAssessment]
    contact_book_recent: list[StudentDetailContactBook]
