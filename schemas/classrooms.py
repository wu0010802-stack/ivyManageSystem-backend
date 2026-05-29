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
    special_needs: Optional[str] = None


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
