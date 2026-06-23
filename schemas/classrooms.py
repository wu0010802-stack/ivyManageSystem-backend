"""Classrooms router (api/classrooms.py) 對應 Out schemas。

Phase 2 範圍：
- POST /classrooms → MutationResultOut
- PUT /classrooms/{id} → ClassroomUpdateResultOut (含 name)
- DELETE /classrooms/{id} → MutationResultOut
- GET /classrooms/teacher-options → list[TeacherOptionOut]
- GET /grades → list[GradeOut]
- PATCH /grades/{id} → GradeOut

Phase 3.5 範圍（本次新增）：
- GET /classrooms → list[ClassroomListItemOut]
- GET /classrooms/{id} → ClassroomDetailOut
- GET /classrooms/{id}/enrollment-composition → ClassroomEnrollmentCompositionOut
- POST /classrooms/clone-term → ClassroomCloneTermResultOut
- POST /classrooms/promote-academic-year → ClassroomPromoteAcademicYearResultOut

PII：
- 老師姓名 (head_teacher_name / assistant_teacher_name / english_teacher_name /
  art_teacher_name) — admin/教師端必看，標 # pii-allow:
- 學生姓名 (student_preview[].name / students[].name) — admin/教師端必看，
  標 # pii-allow:
- 家長電話 (parent_phone) — admin/教師端必看（緊急聯絡用），標 # pii-allow:
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class TeacherOptionOut(IvyBaseModel):
    """GET /classrooms/teacher-options 單筆。"""

    id: int
    name: str  # pii-allow: 老師姓名（教師端必看）


class ClassroomUpdateResultOut(IvyBaseModel):
    """PUT /classrooms/{id} 回傳 — message + id + name。"""

    message: str
    id: int
    name: str


class GradeOut(IvyBaseModel):
    """年級 (GET /grades list 單筆 / PATCH /grades/{id} 回傳)。

    PATCH 不回 age_range / sort_order，這兩欄為 Optional。
    """

    id: int
    name: str
    age_range: Optional[str] = None
    sort_order: Optional[int] = None
    is_graduation_grade: bool


# ──────────────────────────────────────────────────────────────────────
# GET /classrooms → list[ClassroomListItemOut]
# ──────────────────────────────────────────────────────────────────────


class ClassroomStudentPreviewOut(IvyBaseModel):
    """list /classrooms 內單班最多 3 個學生 preview。"""

    id: int
    student_id: Optional[str] = None
    name: str  # pii-allow: 學童姓名（教師端必看）
    gender: Optional[str] = None


class ClassroomListItemOut(IvyBaseModel):
    """GET /classrooms 單筆 — 含老師姓名、學生數與最多 3 筆 preview。"""

    id: int
    name: str
    class_code: Optional[str] = None
    school_year: int
    semester: int
    semester_label: str
    grade_id: Optional[int] = None
    grade_name: Optional[str] = None
    capacity: Optional[int] = None
    current_count: int
    head_teacher_id: Optional[int] = None
    head_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    assistant_teacher_id: Optional[int] = None
    assistant_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    english_teacher_id: Optional[int] = None
    english_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    art_teacher_id: Optional[int] = None
    art_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    student_preview: list[ClassroomStudentPreviewOut]
    has_more_students: bool
    is_active: bool


# ──────────────────────────────────────────────────────────────────────
# GET /classrooms/{id} → ClassroomDetailOut
# ──────────────────────────────────────────────────────────────────────


class ClassroomDetailStudentOut(IvyBaseModel):
    """GET /classrooms/{id} 內學生單筆（健康欄位由 router 端依權限遮罩成 None）。"""

    id: int
    student_id: Optional[str] = None
    name: str  # pii-allow: 學童姓名（教師端必看）
    gender: Optional[str] = None
    parent_phone: Optional[str] = None  # pii-allow: 家長電話（教師端緊急聯絡必看）
    status: Optional[str] = None
    is_active: Optional[bool] = None
    allergy: Optional[str] = (
        None  # pii-allow: 健康欄位（router 端依 STUDENTS_HEALTH_READ 遮罩）
    )
    medication: Optional[str] = (
        None  # pii-allow: 健康欄位（router 端依 STUDENTS_HEALTH_READ 遮罩）
    )
    special_needs: Optional[str] = (
        None  # pii-allow: 特殊需求（router 端依 STUDENTS_HEALTH_READ 遮罩）
    )


class ClassroomDetailOut(IvyBaseModel):
    """GET /classrooms/{id} — 單班詳細含學生列表。"""

    id: int
    name: str
    class_code: Optional[str] = None
    school_year: int
    semester: int
    semester_label: str
    grade_id: Optional[int] = None
    grade_name: Optional[str] = None
    capacity: Optional[int] = None
    current_count: int
    head_teacher_id: Optional[int] = None
    head_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    assistant_teacher_id: Optional[int] = None
    assistant_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    english_teacher_id: Optional[int] = None
    english_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    art_teacher_id: Optional[int] = None
    art_teacher_name: Optional[str] = None  # pii-allow: 老師姓名（教師端必看）
    students: list[ClassroomDetailStudentOut]
    is_active: bool


# ──────────────────────────────────────────────────────────────────────
# GET /classrooms/{id}/enrollment-composition
# ──────────────────────────────────────────────────────────────────────


class ClassroomEnrollmentCompositionOut(IvyBaseModel):
    """GET /classrooms/{id}/enrollment-composition — 在籍特殊身分比例。

    counts / ratios 為 dict[str, X]，key 為中文 status_tag（新生/不足齡/特教生
    /原住民）。Pydantic field 不可中文 identifier，用 dict 接住對應已落地的
    其他統計（appraisal/activity_admin 同 pattern）。timeline 留給 v2，目前
    永遠回傳空 list。
    """

    classroom_id: int
    snapshot_date: str
    total: int
    counts: dict[str, int]
    ratios: dict[str, float]
    timeline: list[dict]


# ──────────────────────────────────────────────────────────────────────
# POST /classrooms/clone-term → ClassroomCloneTermResultOut
# ──────────────────────────────────────────────────────────────────────


class ClassroomCloneTermResultOut(IvyBaseModel):
    """POST /classrooms/clone-term — 學期間複製班級結果。"""

    message: str
    created_count: int
    target_term: str


# ──────────────────────────────────────────────────────────────────────
# POST /classrooms/promote-academic-year → ClassroomPromoteAcademicYearResultOut
# ──────────────────────────────────────────────────────────────────────


class ClassroomPromoteAcademicYearResultOut(IvyBaseModel):
    """POST /classrooms/promote-academic-year — 跨學年升班結果。

    含三個獨立 count：created（新建班級數）、moved_student（移轉學生數）、
    graduated（畢業學生數，無 target_grade 時自動觸發）。
    """

    message: str
    created_count: int
    moved_student_count: int
    graduated_count: int
    target_term: str


# ──────────────────────────────────────────────────────────────────────
# POST /classrooms/promote-academic-year/preview → ClassroomPromotePreviewOut
# ──────────────────────────────────────────────────────────────────────


class ClassroomPromotePreviewRowOut(IvyBaseModel):
    """升班預覽：單一來源班級的處置結果（不寫入，僅試算）。"""

    source_classroom_id: int
    source_name: str
    source_grade_id: Optional[int] = None
    source_grade_name: Optional[str] = None
    resolved_target_grade_id: Optional[int] = None  # None = 畢業
    resolved_target_grade_name: Optional[str] = None
    target_name: Optional[str] = None  # 回放使用者輸入；畢業列為 None
    will_graduate: bool
    active_student_count: int  # 該來源班在讀學生數
    reuses_existing_target: bool  # 命中可重用的停用班級


class ClassroomPromoteConflictOut(IvyBaseModel):
    """升班預覽：逐班層級的阻擋性衝突。

    kind ∈ {missing_source, missing_target_name, duplicate_target_name,
    active_name_collision, invalid_target_grade, reusable_target_has_students}。
    """

    kind: str
    source_classroom_id: Optional[int] = None
    target_name: Optional[str] = None
    message: str  # 已本地化的中文訊息


class ClassroomPromotePreviewOut(IvyBaseModel):
    """POST /classrooms/promote-academic-year/preview — 升班試算（不寫入）。

    rows 為逐班處置；conflicts 收集所有阻擋性問題（execute 會於非空時拒絕）。
    三個 count 與 execute 實際結果在「全員乾淨在讀」前提下相等。
    """

    source_term: str
    target_term: str
    rows: list[ClassroomPromotePreviewRowOut]
    will_create_count: int
    will_move_student_count: int
    will_graduate_count: int
    conflicts: list[ClassroomPromoteConflictOut]
    has_blocking_conflict: bool
