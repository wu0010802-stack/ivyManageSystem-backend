"""Students router (api/students.py) 對應 Out schemas。

Phase 2 範圍（既已 ship）：
- GuardianOut / GuardianListOut（_serialize_guardian shape）
- POST /students/{id}/guardians → GuardianOut
- PATCH /students/guardians/{id} → GuardianOut
- DELETE /students/guardians/{id} → MutationResultOut (re-use)
- GET /students/{id}/guardians → GuardianListOut
- POST /students / PUT /students/{id} / DELETE /students/{id} / POST graduate /
  POST lifecycle 共用 MutationResultOut (re-use from _common)。

Phase 3.5 範圍（本檔追加）：
- GET /students → StudentListOut (StudentListItemOut + 分頁)
- GET /students/records → StudentRecordsTimelineOut (StudentRecordTimelineItem + 分頁)
- GET /students/{id}/academic-summary → AcademicSummaryOut
- GET /students/{id} → StudentDetailOut
- POST /students/bulk-transfer → BulkTransferResultOut（非 MutationResultOut shape）

Out of scope (defer 到 Phase 3+)：
- GET /students/{id}/profile (assemble_profile 10 個 sub-block helper 聚合
  + polymorphic timeline，~200 行 modeling cost；既有 portal_students
  StudentDetailOut 已 cover 大部分視覺化情境)
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel


class GuardianOut(IvyBaseModel):
    """單筆監護人資料 (對應 _serialize_guardian)。"""

    id: int
    student_id: int
    name: Optional[str] = None  # pii-allow: 監護人姓名
    phone: Optional[str] = None  # pii-allow: 監護人電話
    email: Optional[str] = None  # pii-allow: 監護人 email
    relation: Optional[str] = None
    is_primary: bool
    is_emergency: bool
    can_pickup: bool
    custody_note: Optional[str] = None  # pii-allow: 監護權備註
    sort_order: Optional[int] = None


class GuardianListOut(IvyBaseModel):
    """GET /students/{id}/guardians 回傳 — {items}。"""

    items: list[GuardianOut]


# ──────────────────────────────────────────────────────────────────────
# GET /students — 學生列表（分頁）
# ──────────────────────────────────────────────────────────────────────


class StudentListItemOut(IvyBaseModel):
    """GET /students items 內單筆學生欄位。

    `allergy` / `medication` / `special_needs` 在缺權限時會被 router 端
    `mask_student_health_fields` 設為 None（不刪除 key）；故型別維持 Optional[str]。
    """

    id: int
    student_id: str
    name: str  # pii-allow: 學童姓名（行政端必看）
    gender: Optional[str] = None
    birthday: Optional[str] = None  # pii-allow: 生日 ISO 字串
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    graduation_date: Optional[str] = None
    status: Optional[str] = None
    parent_name: Optional[str] = None  # pii-allow: 監護人姓名
    parent_phone: Optional[str] = None  # pii-allow: 監護人電話
    address: Optional[str] = None  # pii-allow: 家庭住址
    status_tag: Optional[str] = None
    allergy: Optional[str] = None  # pii-allow: 過敏（health 敏感）
    medication: Optional[str] = None  # pii-allow: 用藥（health 敏感）
    special_needs: Optional[str] = (
        None  # pii-allow: 特殊需求（health 敏感，router 端依權限遮罩）
    )
    emergency_contact_name: Optional[str] = None  # pii-allow: 緊急聯絡人姓名
    emergency_contact_phone: Optional[str] = None  # pii-allow: 緊急聯絡人電話
    emergency_contact_relation: Optional[str] = (
        None  # pii-allow: 緊急聯絡關係（與緊急聯絡人成對）
    )
    is_active: bool


class StudentListOut(IvyBaseModel):
    """GET /students 分頁回應。"""

    items: list[StudentListItemOut]
    total: int
    skip: int
    limit: int


# ──────────────────────────────────────────────────────────────────────
# GET /students/{id} — 學生詳細資料（admin/teacher 視角）
# ──────────────────────────────────────────────────────────────────────


class StudentDetailOut(IvyBaseModel):
    """GET /students/{id} 詳細回傳。

    欄位集與 StudentListItemOut 有重疊但**不相同**：
    - detail 帶 `notes`；list 不帶
    - list 帶 `graduation_date` / `status` / `status_tag`；detail 不帶
    """

    id: int
    student_id: str
    name: str  # pii-allow: 學童姓名（行政端必看）
    gender: Optional[str] = None
    birthday: Optional[str] = None  # pii-allow: 生日 ISO 字串
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    enrollment_school_year: Optional[int] = None
    enrollment_semester: Optional[int] = None
    parent_name: Optional[str] = None  # pii-allow: 監護人姓名
    parent_phone: Optional[str] = None  # pii-allow: 監護人電話
    address: Optional[str] = None  # pii-allow: 家庭住址
    notes: Optional[str] = None
    allergy: Optional[str] = None  # pii-allow: 過敏（health 敏感）
    medication: Optional[str] = None  # pii-allow: 用藥（health 敏感）
    special_needs: Optional[str] = (
        None  # pii-allow: 特殊需求（health 敏感，router 端依權限遮罩）
    )
    emergency_contact_name: Optional[str] = None  # pii-allow: 緊急聯絡人姓名
    emergency_contact_phone: Optional[str] = None  # pii-allow: 緊急聯絡人電話
    emergency_contact_relation: Optional[str] = (
        None  # pii-allow: 緊急聯絡關係（與緊急聯絡人成對）
    )
    is_active: bool


# ──────────────────────────────────────────────────────────────────────
# GET /students/{id}/academic-summary — 教務指標摘要
# ──────────────────────────────────────────────────────────────────────


class AcademicSummaryOut(IvyBaseModel):
    """GET /students/{id}/academic-summary 回傳。

    `period` 內鍵名為 `from`（python 保留字）— router 用 dict literal 直接寫，
    在 Pydantic 用 `Field(alias="from")` 處理。
    """

    school_year: int
    semester: int
    period: dict[
        str, str
    ]  # {"from": "...", "to": "..."} — 保留 raw dict 避 alias 複雜度
    attendance_rate: float
    attendance_total: int
    attendance_present: int
    leave_days: int
    assessment_count: int
    incident_count: int


# ──────────────────────────────────────────────────────────────────────
# GET /students/records — 跨三模型統一時間軸（incident / assessment / change_log）
# ──────────────────────────────────────────────────────────────────────


class StudentRecordTimelineItem(IvyBaseModel):
    """單筆時間軸項目；`payload` 因三型異質保留 raw dict。"""

    record_type: str  # "incident" / "assessment" / "change_log"
    record_id: int
    occurred_at: Optional[str] = None  # ISO 字串（router 已序列化）
    student_id: int
    student_name: Optional[str] = None  # pii-allow: 學童姓名（行政端必看）
    classroom_id: Optional[int] = None
    classroom_name: Optional[str] = None
    summary: Optional[str] = None
    severity: Optional[str] = None
    parent_notified: Optional[bool] = None
    payload: dict[str, Any]  # incident / assessment / change_log 各自不同 shape


class StudentRecordsTimelineOut(IvyBaseModel):
    """GET /students/records 分頁回應。"""

    items: list[StudentRecordTimelineItem]
    total: int
    page: int
    page_size: int


# ──────────────────────────────────────────────────────────────────────
# POST /students/bulk-transfer — 批次轉班
# ──────────────────────────────────────────────────────────────────────


class BulkTransferResultOut(IvyBaseModel):
    """POST /students/bulk-transfer 回傳；shape 與 MutationResultOut 不同
    （沒 `id`，多了 `moved_count` + `target_classroom_id`）。"""

    message: str
    moved_count: int
    target_classroom_id: int


class BulkGraduateSkippedItem(IvyBaseModel):
    """bulk-graduate 跳過的學生（找不到 / 已非在讀 / 離園日早於入學日）。"""

    student_id: int
    reason: str


class BulkGraduateResultOut(IvyBaseModel):
    """POST /students/bulk-graduate 回傳：有效在讀學生原子處理，無效者列入 skipped。"""

    message: str
    status: str
    graduated_count: int
    succeeded_ids: list[int]
    skipped: list[BulkGraduateSkippedItem]
