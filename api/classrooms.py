"""
Classroom management router
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import date
from utils.taipei_time import today_taipei, now_taipei_naive

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from utils.errors import raise_safe_500
from utils.etag import etag_response
from pydantic import BaseModel, Field, field_validator, model_validator

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from models.database import get_session, Classroom, ClassGrade, Employee, Student
from models.classroom import LIFECYCLE_GRADUATED
from models.student_transfer import StudentClassroomTransfer
from services.student_lifecycle import (
    transition as lifecycle_transition,
    LifecycleTransitionError,
)
from services.graduation_scheduler import graduation_date_for_year
from schemas._common import MutationResultOut
from schemas.classrooms import (
    ClassroomCloneTermResultOut,
    ClassroomDetailOut,
    ClassroomEnrollmentCompositionOut,
    ClassroomListItemOut,
    ClassroomPromoteAcademicYearResultOut,
    ClassroomPromoteConflictOut,
    ClassroomPromotePreviewOut,
    ClassroomPromotePreviewRowOut,
    ClassroomUpdateResultOut,
    GradeOut,
    TeacherOptionOut,
)
from utils.academic import resolve_current_academic_term, resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.error_messages import CLASSROOM_NOT_FOUND
from utils.permissions import Permission
from utils.portfolio_access import mask_student_health_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["classrooms"])


# ============ Pydantic Models ============


class ClassroomCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    class_code: Optional[str] = Field(None, max_length=20)
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)
    # 年級為必填：缺漏會導致該班無法掛入「在籍統計」（後端 SQL JOIN ClassGrade）
    grade_id: int = Field(..., ge=1)
    capacity: int = Field(30, ge=1, le=200)
    head_teacher_id: Optional[int] = Field(None, ge=1)
    assistant_teacher_id: Optional[int] = Field(None, ge=1)
    english_teacher_id: Optional[int] = Field(
        None, ge=1, description="對外標準欄位，對應 legacy art_teacher_id"
    )
    art_teacher_id: Optional[int] = Field(None, ge=1)
    is_active: bool = True

    @field_validator("name", "class_code", mode="before")
    @classmethod
    def strip_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("class_code")
    @classmethod
    def empty_class_code_as_none(cls, value):
        return value or None

    @model_validator(mode="before")
    @classmethod
    def map_english_teacher_field(cls, data):
        if isinstance(data, dict) and "english_teacher_id" in data:
            data["art_teacher_id"] = data.get("english_teacher_id")
        return data


class ClassroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    class_code: Optional[str] = Field(None, max_length=20)
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)
    grade_id: Optional[int] = Field(None, ge=1)
    capacity: Optional[int] = Field(None, ge=1, le=200)
    head_teacher_id: Optional[int] = Field(None, ge=1)
    assistant_teacher_id: Optional[int] = Field(None, ge=1)
    english_teacher_id: Optional[int] = Field(
        None, ge=1, description="對外標準欄位，對應 legacy art_teacher_id"
    )
    art_teacher_id: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None

    @field_validator("name", "class_code", mode="before")
    @classmethod
    def strip_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("class_code")
    @classmethod
    def empty_class_code_as_none(cls, value):
        return value or None

    @model_validator(mode="before")
    @classmethod
    def map_english_teacher_field(cls, data):
        if isinstance(data, dict) and "english_teacher_id" in data:
            data["art_teacher_id"] = data.get("english_teacher_id")
        return data


class ClassroomCloneTerm(BaseModel):
    source_school_year: int = Field(..., ge=100, le=200)
    source_semester: int = Field(..., ge=1, le=2)
    target_school_year: int = Field(..., ge=100, le=200)
    target_semester: int = Field(..., ge=1, le=2)
    copy_teachers: bool = True


class ClassroomPromotionItem(BaseModel):
    source_classroom_id: int = Field(..., ge=1)
    target_name: Optional[str] = Field(None, min_length=1, max_length=50)
    target_grade_id: Optional[int] = Field(None, ge=1)
    copy_teachers: bool = True
    move_students: bool = True

    @field_validator("target_name", mode="before")
    @classmethod
    def strip_target_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
        return value


class ClassroomPromoteAcademicYear(BaseModel):
    source_school_year: int = Field(..., ge=100, le=200)
    source_semester: int = Field(..., ge=1, le=2)
    target_school_year: int = Field(..., ge=100, le=200)
    target_semester: int = Field(..., ge=1, le=2)
    classrooms: list[ClassroomPromotionItem] = Field(..., min_length=1)


# ============ Helpers ============

SEMESTER_LABELS = {
    1: "上學期",
    2: "下學期",
}


def _semester_label(school_year: int, semester: int) -> str:
    return f"{school_year}學年度{SEMESTER_LABELS.get(semester, str(semester))}"


def _sync_employee_classroom_id(session, employee_ids: list[int]) -> None:
    """重新計算指定員工的 Employee.classroom_id（取自當期 active classroom）。

    Why: Employee.classroom_id 是冗餘欄位，過去班級頁面更新老師時並未同步寫回，
    造成薪資引擎、薪資匯出、員工 API 全面失準（沉默歸零最危險）。本函式為單一
    更新點，會在所有班級異動端點被呼叫，重新依當期 active 班級的 head/assistant/
    art_teacher_id 計算每位受影響員工的 classroom_id。

    優先序：head_teacher > assistant_teacher > art_teacher > 班級 id 較小者。
    跨學期時取「當前學期」班級為準（與 utils.academic.resolve_current_academic_term
    一致），其他學期班級不影響此值。
    """
    if not employee_ids:
        return

    school_year, semester = resolve_current_academic_term()
    classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.is_active == True,
            Classroom.school_year == school_year,
            Classroom.semester == semester,
        )
        .order_by(Classroom.id.asc())
        .all()
    )

    def _pick(emp_id: int) -> Optional[int]:
        head = next((c for c in classrooms if c.head_teacher_id == emp_id), None)
        if head:
            return head.id
        assistant = next(
            (c for c in classrooms if c.assistant_teacher_id == emp_id), None
        )
        if assistant:
            return assistant.id
        art = next((c for c in classrooms if c.art_teacher_id == emp_id), None)
        return art.id if art else None

    employees = session.query(Employee).filter(Employee.id.in_(set(employee_ids))).all()
    for emp in employees:
        new_id = _pick(emp.id)
        if emp.classroom_id != new_id:
            emp.classroom_id = new_id


def _classroom_teacher_ids(classroom: Optional[Classroom]) -> list[int]:
    """從 Classroom 取出三個教師欄位中非空的 id 清單，便於同步呼叫。"""
    if classroom is None:
        return []
    return [
        tid
        for tid in (
            classroom.head_teacher_id,
            classroom.assistant_teacher_id,
            classroom.art_teacher_id,
        )
        if tid
    ]


def _validate_distinct_teacher_assignments(
    head_teacher_id: Optional[int],
    assistant_teacher_id: Optional[int],
    art_teacher_id: Optional[int],
):
    teacher_ids = [
        teacher_id
        for teacher_id in (head_teacher_id, assistant_teacher_id, art_teacher_id)
        if teacher_id is not None
    ]
    if len(teacher_ids) != len(set(teacher_ids)):
        raise HTTPException(
            status_code=400, detail="同一位老師不可同時擔任同班多個角色"
        )


def _validate_grade_exists(session, grade_id: Optional[int]):
    if grade_id is None:
        return
    grade = (
        session.query(ClassGrade.id)
        .filter(
            ClassGrade.id == grade_id,
            ClassGrade.is_active == True,
        )
        .first()
    )
    if not grade:
        raise HTTPException(status_code=400, detail="指定的年級不存在或已停用")


def _validate_teacher_ids(session, teacher_ids: list[int]):
    if not teacher_ids:
        return
    teachers = (
        session.query(Employee.id)
        .filter(
            Employee.id.in_(teacher_ids),
            Employee.is_active == True,
        )
        .all()
    )
    existing_ids = {teacher.id for teacher in teachers}
    missing_ids = [
        teacher_id for teacher_id in teacher_ids if teacher_id not in existing_ids
    ]
    if missing_ids:
        raise HTTPException(
            status_code=400, detail=f"指定的教師不存在或已停用: {missing_ids}"
        )


def _validate_unique_classroom(
    session,
    name: Optional[str],
    class_code: Optional[str],
    school_year: int,
    semester: int,
    classroom_id: Optional[int] = None,
):
    if name:
        q = session.query(Classroom.id).filter(
            func.lower(Classroom.name) == name.lower(),
            Classroom.school_year == school_year,
            Classroom.semester == semester,
        )
        if classroom_id is not None:
            q = q.filter(Classroom.id != classroom_id)
        if q.first():
            raise HTTPException(status_code=400, detail="班級名稱已存在")

    if class_code:
        q = session.query(Classroom.id).filter(
            func.lower(Classroom.class_code) == class_code.lower(),
            Classroom.school_year == school_year,
            Classroom.semester == semester,
        )
        if classroom_id is not None:
            q = q.filter(Classroom.id != classroom_id)
        if q.first():
            raise HTTPException(status_code=400, detail="班級代號已存在")


def _get_grade_map(session) -> dict[int, ClassGrade]:
    grades = session.query(ClassGrade).filter(ClassGrade.is_active == True).all()
    return {grade.id: grade for grade in grades}


def _should_advance_grade(
    source_school_year: int,
    source_semester: int,
    target_school_year: int,
    target_semester: int,
) -> bool:
    return (
        source_semester == 2
        and target_semester == 1
        and target_school_year > source_school_year
    )


def _resolve_next_grade_id(
    source_classroom: Classroom,
    grade_map: dict[int, ClassGrade],
    *,
    source_school_year: int,
    source_semester: int,
    target_school_year: int,
    target_semester: int,
) -> Optional[int]:
    if not source_classroom.grade_id:
        return None
    if not _should_advance_grade(
        source_school_year, source_semester, target_school_year, target_semester
    ):
        return source_classroom.grade_id
    source_grade = grade_map.get(source_classroom.grade_id)
    if not source_grade:
        return None
    next_grade = next(
        (
            grade
            for grade in grade_map.values()
            if grade.sort_order == source_grade.sort_order - 1
        ),
        None,
    )
    return next_grade.id if next_grade else None


def _term_start_date(school_year: int, semester: int) -> date:
    """回傳學期開始日期。school_year 為民國年，需轉換為西元年。"""
    western_year = school_year + 1911
    if semester == 1:
        return date(western_year, 8, 1)
    return date(western_year + 1, 2, 1)


def _serialize_classroom_detail(
    session, classroom: Classroom, current_user: Optional[dict] = None
):
    # Classroom.grade 已透過 joinedload 預載，直接存取即可
    grade_name = classroom.grade.name if classroom.grade else None

    teacher_ids = [
        tid
        for tid in (
            classroom.head_teacher_id,
            classroom.assistant_teacher_id,
            classroom.art_teacher_id,
        )
        if tid
    ]
    teacher_map = {}
    if teacher_ids:
        teachers = (
            session.query(Employee.id, Employee.name)
            .filter(Employee.id.in_(teacher_ids))
            .all()
        )
        teacher_map = {t.id: t.name for t in teachers}

    students = (
        session.query(Student)
        .filter(
            Student.classroom_id == classroom.id,
        )
        .order_by(
            Student.is_active.desc(),
            Student.name,
        )
        .all()
    )

    student_list = []
    for s in students:
        row = {
            "id": s.id,
            "student_id": s.student_id,
            "name": s.name,
            "gender": s.gender,
            "parent_phone": s.parent_phone,
            "status": s.status or ("在讀中" if s.is_active is not False else "未設定"),
            "is_active": s.is_active,
            "allergy": s.allergy,
            "medication": s.medication,
            "special_needs": s.special_needs,
        }
        if current_user is not None:
            row = mask_student_health_fields(row, current_user)
        student_list.append(row)

    # is_active = NULL 視為在讀（歷史資料無明確設定時的預設行為）
    active_count = sum(1 for s in students if s.is_active is not False)

    return {
        "id": classroom.id,
        "name": classroom.name,
        "class_code": classroom.class_code,
        "school_year": classroom.school_year,
        "semester": classroom.semester,
        "semester_label": _semester_label(classroom.school_year, classroom.semester),
        "grade_id": classroom.grade_id,
        "grade_name": grade_name,
        "capacity": classroom.capacity,
        "current_count": active_count,
        "head_teacher_id": classroom.head_teacher_id,
        "head_teacher_name": teacher_map.get(classroom.head_teacher_id),
        "assistant_teacher_id": classroom.assistant_teacher_id,
        "assistant_teacher_name": teacher_map.get(classroom.assistant_teacher_id),
        "english_teacher_id": classroom.art_teacher_id,
        "english_teacher_name": teacher_map.get(classroom.art_teacher_id),
        "art_teacher_id": classroom.art_teacher_id,
        "art_teacher_name": teacher_map.get(classroom.art_teacher_id),
        "students": student_list,
        "is_active": classroom.is_active,
    }


# ============ Routes ============


@router.get("/classrooms", response_model=list[ClassroomListItemOut])
def get_classrooms(
    request: Request,
    response: Response,
    include_inactive: bool = Query(False),
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_only: bool = Query(True),
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_READ)),
):
    """取得所有班級列表（含老師和學生數）"""
    session = get_session()
    try:
        q = session.query(Classroom)
        if school_year is not None or semester is not None:
            resolved_school_year, resolved_semester = resolve_academic_term_filters(
                school_year, semester
            )
            q = q.filter(
                Classroom.school_year == resolved_school_year,
                Classroom.semester == resolved_semester,
            )
        elif current_only:
            resolved_school_year, resolved_semester = resolve_current_academic_term()
            q = q.filter(
                Classroom.school_year == resolved_school_year,
                Classroom.semester == resolved_semester,
            )
        if not include_inactive:
            q = q.filter(Classroom.is_active == True)
        classrooms = q.order_by(
            Classroom.school_year.desc(),
            Classroom.semester,
            Classroom.is_active.desc(),
            Classroom.id,
        ).all()
        if not classrooms:
            return []

        # 批量載入年級
        grade_ids = {c.grade_id for c in classrooms if c.grade_id}
        grade_map = {}
        if grade_ids:
            grades = (
                session.query(ClassGrade).filter(ClassGrade.id.in_(grade_ids)).all()
            )
            grade_map = {g.id: g.name for g in grades}

        # 批量載入老師
        teacher_ids = set()
        for c in classrooms:
            for tid in (c.head_teacher_id, c.assistant_teacher_id, c.art_teacher_id):
                if tid:
                    teacher_ids.add(tid)
        teacher_map = {}
        if teacher_ids:
            teachers = (
                session.query(Employee.id, Employee.name)
                .filter(Employee.id.in_(teacher_ids))
                .all()
            )
            teacher_map = {t.id: t.name for t in teachers}

        # 批量取得各班學生數（單一聚合查詢）
        classroom_ids = [c.id for c in classrooms]
        student_counts = (
            session.query(Student.classroom_id, func.count(Student.id))
            .filter(Student.classroom_id.in_(classroom_ids), Student.is_active == True)
            .group_by(Student.classroom_id)
            .all()
        )
        count_map = dict(student_counts)

        preview_rows = (
            session.query(
                Student.classroom_id,
                Student.id,
                Student.student_id,
                Student.name,
                Student.gender,
            )
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active == True,
            )
            .order_by(
                Student.classroom_id,
                Student.name,
            )
            .all()
        )

        preview_map: dict[int, list[dict]] = {}
        for row in preview_rows:
            classroom_preview = preview_map.setdefault(row.classroom_id, [])
            if len(classroom_preview) >= 3:
                continue
            classroom_preview.append(
                {
                    "id": row.id,
                    "student_id": row.student_id,
                    "name": row.name,
                    "gender": row.gender,
                }
            )

        result = []
        for c in classrooms:
            student_preview = preview_map.get(c.id, [])
            result.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "class_code": c.class_code,
                    "school_year": c.school_year,
                    "semester": c.semester,
                    "semester_label": _semester_label(c.school_year, c.semester),
                    "grade_id": c.grade_id,
                    "grade_name": grade_map.get(c.grade_id),
                    "capacity": c.capacity,
                    "current_count": count_map.get(c.id, 0),
                    "head_teacher_id": c.head_teacher_id,
                    "head_teacher_name": teacher_map.get(c.head_teacher_id),
                    "assistant_teacher_id": c.assistant_teacher_id,
                    "assistant_teacher_name": teacher_map.get(c.assistant_teacher_id),
                    "english_teacher_id": c.art_teacher_id,
                    "english_teacher_name": teacher_map.get(c.art_teacher_id),
                    "art_teacher_id": c.art_teacher_id,
                    "art_teacher_name": teacher_map.get(c.art_teacher_id),
                    "student_preview": student_preview,
                    "has_more_students": count_map.get(c.id, 0) > len(student_preview),
                    "is_active": c.is_active,
                }
            )
        return etag_response(request, response, result)
    finally:
        session.close()


@router.get("/classrooms/teacher-options", response_model=list[TeacherOptionOut])
def get_teacher_options(
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_READ)),
):
    """取得可指派教師清單。"""
    session = get_session()
    try:
        teachers = (
            session.query(Employee.id, Employee.name)
            .filter(Employee.is_active == True)
            .order_by(Employee.name)
            .all()
        )
        return [{"id": teacher.id, "name": teacher.name} for teacher in teachers]
    finally:
        session.close()


@router.get("/classrooms/{classroom_id}", response_model=ClassroomDetailOut)
def get_classroom(
    classroom_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_READ)),
):
    """取得單一班級詳細資料（含學生列表）"""
    session = get_session()
    try:
        classroom = (
            session.query(Classroom)
            .options(joinedload(Classroom.grade))
            .filter(Classroom.id == classroom_id)
            .first()
        )
        if not classroom:
            raise HTTPException(status_code=404, detail=CLASSROOM_NOT_FOUND)
        return _serialize_classroom_detail(session, classroom, current_user)
    finally:
        session.close()


@router.get("/classrooms/{classroom_id}/enrollment-composition", response_model=ClassroomEnrollmentCompositionOut)
def get_classroom_enrollment_composition(
    classroom_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_READ)),
):
    """
    取得班級在籍學生的特殊身分比例（當前快照）。

    目前版本只回傳當前快照；未來若需要時間軸（按月點回放），需整合
    StudentChangeLog 的 enter/leave 事件與 status_tag 的歷史變化。
    """
    session = get_session()
    try:
        classroom = (
            session.query(Classroom).filter(Classroom.id == classroom_id).first()
        )
        if not classroom:
            raise HTTPException(status_code=404, detail=CLASSROOM_NOT_FOUND)

        students = (
            session.query(Student)
            .filter(Student.classroom_id == classroom_id, Student.is_active.is_(True))
            .all()
        )

        counts = {"新生": 0, "不足齡": 0, "特教生": 0, "原住民": 0}
        for s in students:
            tag = (s.status_tag or "").strip()
            if tag in counts:
                counts[tag] += 1

        total = len(students)
        return {
            "classroom_id": classroom_id,
            "snapshot_date": today_taipei().isoformat(),
            "total": total,
            "counts": counts,
            "ratios": {
                k: round(v / total, 4) if total else 0.0 for k, v in counts.items()
            },
            "timeline": [],  # 留給 v2
        }
    finally:
        session.close()


@router.post("/classrooms", status_code=201, response_model=MutationResultOut)
def create_classroom(
    item: ClassroomCreate,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """新增班級"""
    session = get_session()
    try:
        school_year, semester = resolve_academic_term_filters(
            item.school_year, item.semester
        )
        _validate_distinct_teacher_assignments(
            item.head_teacher_id,
            item.assistant_teacher_id,
            item.art_teacher_id,
        )
        _validate_grade_exists(session, item.grade_id)
        _validate_teacher_ids(
            session,
            [
                teacher_id
                for teacher_id in (
                    item.head_teacher_id,
                    item.assistant_teacher_id,
                    item.art_teacher_id,
                )
                if teacher_id is not None
            ],
        )
        _validate_unique_classroom(
            session,
            item.name,
            item.class_code,
            school_year,
            semester,
        )

        payload = item.model_dump(exclude={"english_teacher_id"})
        payload["school_year"] = school_year
        payload["semester"] = semester
        classroom = Classroom(**payload)
        session.add(classroom)
        session.flush()
        _sync_employee_classroom_id(session, _classroom_teacher_ids(classroom))
        session.commit()
        return {"message": "班級新增成功", "id": classroom.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級新增失敗")
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()


@router.put("/classrooms/{classroom_id}", response_model=ClassroomUpdateResultOut)
def update_classroom(
    classroom_id: int,
    item: ClassroomUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """更新班級資料"""
    session = get_session()
    try:
        classroom = (
            session.query(Classroom).filter(Classroom.id == classroom_id).first()
        )
        if not classroom:
            raise HTTPException(status_code=404, detail=CLASSROOM_NOT_FOUND)

        update_data = item.model_dump(
            exclude_unset=True, exclude={"english_teacher_id"}
        )
        before_snapshot = {
            k: getattr(classroom, k, None)
            for k in update_data.keys()
            if hasattr(classroom, k)
        }
        # 變更前後的教師 id 集合都要納入同步範圍：被指派老師需新建立 classroom_id，
        # 被取消指派的舊老師也要清除（或重指向其他班）。
        affected_teacher_ids: set[int] = set(
            tid
            for tid in (
                classroom.head_teacher_id,
                classroom.assistant_teacher_id,
                classroom.art_teacher_id,
            )
            if tid
        )
        school_year = update_data.get("school_year", classroom.school_year)
        semester = update_data.get("semester", classroom.semester)
        resolve_academic_term_filters(school_year, semester)

        head_teacher_id = update_data.get("head_teacher_id", classroom.head_teacher_id)
        assistant_teacher_id = update_data.get(
            "assistant_teacher_id", classroom.assistant_teacher_id
        )
        art_teacher_id = update_data.get("art_teacher_id", classroom.art_teacher_id)
        _validate_distinct_teacher_assignments(
            head_teacher_id, assistant_teacher_id, art_teacher_id
        )

        if "grade_id" in update_data:
            _validate_grade_exists(session, update_data["grade_id"])

        _validate_teacher_ids(
            session,
            [
                teacher_id
                for teacher_id in (
                    head_teacher_id,
                    assistant_teacher_id,
                    art_teacher_id,
                )
                if teacher_id is not None
            ],
        )

        _validate_unique_classroom(
            session,
            update_data.get("name"),
            update_data.get("class_code"),
            school_year,
            semester,
            classroom_id=classroom.id,
        )

        NULLABLE_FIELDS = {
            "grade_id",
            "head_teacher_id",
            "assistant_teacher_id",
            "art_teacher_id",
            "class_code",
        }
        for key, value in update_data.items():
            if value is not None or key in NULLABLE_FIELDS:
                setattr(classroom, key, value)

        # 把變更後仍掛在班級的教師也納入同步集合
        affected_teacher_ids.update(
            tid
            for tid in (
                classroom.head_teacher_id,
                classroom.assistant_teacher_id,
                classroom.art_teacher_id,
            )
            if tid
        )
        session.flush()
        _sync_employee_classroom_id(session, sorted(affected_teacher_ids))
        session.commit()

        diff = {}
        for k, old_val in before_snapshot.items():
            new_val = getattr(classroom, k, None)
            if old_val != new_val:
                diff[k] = {"before": old_val, "after": new_val}
        if diff:
            request.state.audit_changes = diff

        return {"message": "班級更新成功", "id": classroom.id, "name": classroom.name}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級更新失敗 classroom_id=%s", classroom_id)
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.post("/classrooms/clone-term", status_code=201, response_model=ClassroomCloneTermResultOut)
def clone_classrooms_to_term(
    item: ClassroomCloneTerm,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """將指定學期的班級複製到另一個學期。"""
    session = get_session()
    try:
        if (
            item.source_school_year == item.target_school_year
            and item.source_semester == item.target_semester
        ):
            raise HTTPException(status_code=400, detail="來源學期與目標學期不可相同")

        source_classrooms = (
            session.query(Classroom)
            .filter(
                Classroom.school_year == item.source_school_year,
                Classroom.semester == item.source_semester,
                Classroom.is_active == True,
            )
            .order_by(Classroom.id)
            .all()
        )
        if not source_classrooms:
            raise HTTPException(status_code=404, detail="來源學期沒有可複製的啟用班級")

        source_names = [classroom.name for classroom in source_classrooms]
        existing_targets = (
            session.query(Classroom.name)
            .filter(
                Classroom.school_year == item.target_school_year,
                Classroom.semester == item.target_semester,
                func.lower(Classroom.name).in_([name.lower() for name in source_names]),
            )
            .all()
        )
        if existing_targets:
            conflicted_names = "、".join(sorted({row.name for row in existing_targets}))
            raise HTTPException(
                status_code=409,
                detail=f"目標學期已存在相同班級：{conflicted_names}",
            )

        created = []
        affected_teacher_ids: set[int] = set()
        for source in source_classrooms:
            cloned = Classroom(
                name=source.name,
                class_code=source.class_code,
                school_year=item.target_school_year,
                semester=item.target_semester,
                grade_id=source.grade_id,
                capacity=source.capacity,
                head_teacher_id=source.head_teacher_id if item.copy_teachers else None,
                assistant_teacher_id=(
                    source.assistant_teacher_id if item.copy_teachers else None
                ),
                art_teacher_id=source.art_teacher_id if item.copy_teachers else None,
                is_active=True,
            )
            session.add(cloned)
            created.append(cloned)
            if item.copy_teachers:
                affected_teacher_ids.update(_classroom_teacher_ids(cloned))

        # 若 copy_teachers=True 且目標學期=當前學期，需同步 Employee.classroom_id；
        # 目標若為未來學期則 _sync 內當期過濾不會匹配，等學期切換後自動生效。
        session.flush()
        _sync_employee_classroom_id(session, sorted(affected_teacher_ids))
        session.commit()
        return {
            "message": "班級複製成功",
            "created_count": len(created),
            "target_term": _semester_label(
                item.target_school_year, item.target_semester
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception(
            "班級學期複製失敗: %s上 -> %s下",
            item.source_school_year,
            item.target_school_year,
        )
        raise_safe_500(e, context="複製失敗")
    finally:
        session.close()


# 衝突 kind 的優先序：對齊原 execute 的 fail-fast 觸發順序。execute 取序位
# 最前者 map 回對應 status code；改動此清單會改變多衝突 payload 回傳的 status code。
_PROMOTE_CONFLICT_PRIORITY = (
    "missing_source",
    "missing_target_name",
    "duplicate_target_name",
    "active_name_collision",
    "invalid_target_grade",
    "reusable_target_has_students",
)
_PROMOTE_CONFLICT_STATUS = {
    "missing_source": 404,
    "missing_target_name": 400,
    "duplicate_target_name": 409,
    "active_name_collision": 409,
    "invalid_target_grade": 400,
    "reusable_target_has_students": 409,
}


@dataclass
class _PromotionConflict:
    kind: str
    message: str
    source_classroom_id: Optional[int] = None
    target_name: Optional[str] = None


@dataclass
class _PromotionPreparedRow:
    item: ClassroomPromotionItem
    source: Classroom
    resolved_grade_id: Optional[int]
    will_graduate: bool
    source_grade_name: Optional[str]
    resolved_grade_name: Optional[str]
    active_student_count: int
    reusable_target: Optional[Classroom] = None  # 命中可重用停用班；None=新建

    @property
    def reuses_existing_target(self) -> bool:
        return self.reusable_target is not None


@dataclass
class _PromotionPlan:
    prepared_rows: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    will_create_count: int = 0
    will_move_student_count: int = 0
    will_graduate_count: int = 0


def _count_active_students(session, classroom_id: int) -> int:
    return (
        session.query(func.count(Student.id))
        .filter(Student.classroom_id == classroom_id, Student.is_active == True)
        .scalar()
        or 0
    )


def _build_promotion_plan(
    session, item: "ClassroomPromoteAcademicYear"
) -> _PromotionPlan:
    """純讀取試算：驗證 + 年級解析 + 衝突偵測，不寫入 session。

    execute 與 preview 共用此函式以杜絕邏輯漂移。整批層級錯誤（來源=目標
    學期相同）由 caller 於呼叫前處理；此處只收集逐班層級衝突到 plan.conflicts，
    且本函式絕不 session.add / flush / 任何寫入。
    """
    plan = _PromotionPlan()

    source_ids = [row.source_classroom_id for row in item.classrooms]
    source_classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.id.in_(source_ids),
            Classroom.school_year == item.source_school_year,
            Classroom.semester == item.source_semester,
            Classroom.is_active == True,
        )
        .all()
    )
    source_map = {classroom.id: classroom for classroom in source_classrooms}
    missing_source_ids = [cid for cid in source_ids if cid not in source_map]
    if missing_source_ids:
        plan.conflicts.append(
            _PromotionConflict(
                kind="missing_source",
                message=f"找不到來源班級：{missing_source_ids}",
            )
        )

    grade_map = _get_grade_map(session)
    for row in item.classrooms:
        source = source_map.get(row.source_classroom_id)
        if source is None:
            continue  # 缺來源班已記於 conflicts
        resolved_grade_id = row.target_grade_id or _resolve_next_grade_id(
            source,
            grade_map,
            source_school_year=item.source_school_year,
            source_semester=item.source_semester,
            target_school_year=item.target_school_year,
            target_semester=item.target_semester,
        )
        will_graduate = resolved_grade_id is None
        if not will_graduate and not row.target_name:
            plan.conflicts.append(
                _PromotionConflict(
                    kind="missing_target_name",
                    message=f"班級「{source.name}」缺少新班名",
                    source_classroom_id=source.id,
                )
            )
        source_grade = grade_map.get(source.grade_id)
        resolved_grade = grade_map.get(resolved_grade_id) if resolved_grade_id else None
        plan.prepared_rows.append(
            _PromotionPreparedRow(
                item=row,
                source=source,
                resolved_grade_id=resolved_grade_id,
                will_graduate=will_graduate,
                source_grade_name=source_grade.name if source_grade else None,
                resolved_grade_name=resolved_grade.name if resolved_grade else None,
                active_student_count=_count_active_students(session, source.id),
            )
        )

    # 目標班名重複（與原 execute 一致：大小寫敏感）
    target_names = [
        prep.item.target_name
        for prep in plan.prepared_rows
        if not prep.will_graduate and prep.item.target_name
    ]
    if len(target_names) != len(set(target_names)):
        plan.conflicts.append(
            _PromotionConflict(
                kind="duplicate_target_name",
                message="目標學期的班級名稱不可重複",
            )
        )

    # 目標學期既有同名班：active → 衝突；inactive → 可重用
    existing_targets = []
    if target_names:
        existing_targets = (
            session.query(Classroom)
            .filter(
                Classroom.school_year == item.target_school_year,
                Classroom.semester == item.target_semester,
                func.lower(Classroom.name).in_([n.lower() for n in target_names]),
            )
            .all()
        )
    active_conflicts = [t for t in existing_targets if t.is_active]
    reusable_pool = {t.name.lower(): t for t in existing_targets if not t.is_active}
    if active_conflicts:
        conflicted_names = "、".join(sorted({t.name for t in active_conflicts}))
        plan.conflicts.append(
            _PromotionConflict(
                kind="active_name_collision",
                message=f"目標學期已存在相同班級：{conflicted_names}",
            )
        )

    # 逐班（非畢業）：目標年級有效性 + 可重用班是否仍有在讀學生（依原順序）
    for prep in plan.prepared_rows:
        if prep.will_graduate:
            plan.will_graduate_count += prep.active_student_count
            continue
        if prep.resolved_grade_id not in grade_map:
            plan.conflicts.append(
                _PromotionConflict(
                    kind="invalid_target_grade",
                    message="指定的目標年級不存在或已停用",
                    source_classroom_id=prep.source.id,
                    target_name=prep.item.target_name,
                )
            )
        reusable_target = (
            reusable_pool.pop(prep.item.target_name.lower(), None)
            if prep.item.target_name
            else None
        )
        if reusable_target is not None:
            prep.reusable_target = reusable_target
            if _count_active_students(session, reusable_target.id) > 0:
                plan.conflicts.append(
                    _PromotionConflict(
                        kind="reusable_target_has_students",
                        message=(
                            f"目標班級「{reusable_target.name}」仍有在讀學生，無法直接重用"
                        ),
                        source_classroom_id=prep.source.id,
                        target_name=prep.item.target_name,
                    )
                )
        plan.will_create_count += 1
        if prep.item.move_students:
            plan.will_move_student_count += prep.active_student_count

    return plan


def _raise_first_promotion_conflict(plan: _PromotionPlan) -> None:
    """plan 有衝突時依優先序取第一個 raise（保留原 fail-fast 的 status code 行為）。"""
    if not plan.conflicts:
        return
    first = min(plan.conflicts, key=lambda c: _PROMOTE_CONFLICT_PRIORITY.index(c.kind))
    raise HTTPException(
        status_code=_PROMOTE_CONFLICT_STATUS[first.kind], detail=first.message
    )


@router.post(
    "/classrooms/promote-academic-year",
    status_code=201,
    response_model=ClassroomPromoteAcademicYearResultOut,
)
def promote_classrooms_to_academic_year(
    item: ClassroomPromoteAcademicYear,
    request: Request = None,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """跨學年升班：建立新班、沿用老師並搬移在讀學生。

    畢業班（無下一年級）學生改走 lifecycle 狀態機 transition() 落地：寫
    StudentChangeLog 稽核並設 lifecycle_status=graduated（避免被 7/31 自動畢業
    排程重複抓取）；搬班逐人寫 StudentClassroomTransfer 留歷史軌跡。畢業日對齊
    自動畢業排程。整體為單一 transaction（任一步失敗則全部 rollback）。
    """
    session = get_session()
    operator_id = current_user.get("user_id")
    try:
        if (
            item.source_school_year == item.target_school_year
            and item.source_semester == item.target_semester
        ):
            raise HTTPException(status_code=400, detail="來源學期與目標學期不可相同")

        plan = _build_promotion_plan(session, item)
        _raise_first_promotion_conflict(plan)

        created_count = 0
        moved_student_count = 0
        graduated_count = 0
        # 畢業日對齊 7/31 自動畢業排程；school_year 為民國年需 +1911 轉西元年。
        graduation_date = graduation_date_for_year(item.target_school_year + 1911)
        now = now_taipei_naive()

        for prep in plan.prepared_rows:
            source = prep.source
            if prep.will_graduate:
                # 畢業改走 lifecycle 狀態機（寫 StudentChangeLog + 設 graduated），
                # 不再 bulk update legacy 欄位；學生留在原畢業班但 lifecycle 已是
                # 終態，7/31 自動畢業排程不會再重抓。
                graduating_students = (
                    session.query(Student)
                    .filter(
                        Student.classroom_id == source.id,
                        Student.is_active == True,
                    )
                    .all()
                )
                for student in graduating_students:
                    try:
                        lifecycle_transition(
                            session,
                            student,
                            to_status=LIFECYCLE_GRADUATED,
                            effective_date=graduation_date,
                            reason="升班畢業",
                            notes=f"班級升班觸發畢業（{source.name}）",
                            recorded_by=operator_id,
                        )
                        graduated_count += 1
                    except LifecycleTransitionError as exc:
                        logger.warning(
                            "升班畢業略過 student_id=%s：%s", student.id, exc
                        )
                continue

            if prep.reusable_target is not None:
                target_classroom = prep.reusable_target
                target_classroom.class_code = source.class_code
                target_classroom.grade_id = prep.resolved_grade_id
                target_classroom.capacity = source.capacity
                target_classroom.head_teacher_id = (
                    source.head_teacher_id if prep.item.copy_teachers else None
                )
                target_classroom.assistant_teacher_id = (
                    source.assistant_teacher_id if prep.item.copy_teachers else None
                )
                target_classroom.art_teacher_id = (
                    source.art_teacher_id if prep.item.copy_teachers else None
                )
                target_classroom.is_active = True
            else:
                target_classroom = Classroom(
                    name=prep.item.target_name,
                    class_code=source.class_code,
                    school_year=item.target_school_year,
                    semester=item.target_semester,
                    grade_id=prep.resolved_grade_id,
                    capacity=source.capacity,
                    head_teacher_id=(
                        source.head_teacher_id if prep.item.copy_teachers else None
                    ),
                    assistant_teacher_id=(
                        source.assistant_teacher_id if prep.item.copy_teachers else None
                    ),
                    art_teacher_id=(
                        source.art_teacher_id if prep.item.copy_teachers else None
                    ),
                    is_active=True,
                )
                session.add(target_classroom)
                session.flush()
            created_count += 1

            if prep.item.move_students:
                # 逐人搬班並寫 StudentClassroomTransfer（重用 bulk-transfer pattern）；
                # 不呼叫 sync_registrations_on_student_transfer（當期 scope 套用到
                # 未來學期班級會誤改當期才藝報名）。
                moving_students = (
                    session.query(Student)
                    .filter(
                        Student.classroom_id == source.id,
                        Student.is_active == True,
                    )
                    .all()
                )
                for student in moving_students:
                    session.add(
                        StudentClassroomTransfer(
                            student_id=student.id,
                            from_classroom_id=student.classroom_id,
                            to_classroom_id=target_classroom.id,
                            transferred_at=now,
                            transferred_by=operator_id,
                        )
                    )
                    student.classroom_id = target_classroom.id
                    moved_student_count += 1

        # 升班完成後同步所有受影響教師的 Employee.classroom_id：
        # 把來源班教師（取消指派）與目標班教師（新指派）一併重算。
        affected_teacher_ids: set[int] = set()
        for prep in plan.prepared_rows:
            affected_teacher_ids.update(_classroom_teacher_ids(prep.source))
        # 目標班教師：源班教師若 copy_teachers 已重複；額外把已落地的 reusable/new
        # target classrooms 上的教師也涵蓋（包含 copy_teachers=False 時的清空案例）。
        target_teacher_rows = (
            session.query(
                Classroom.head_teacher_id,
                Classroom.assistant_teacher_id,
                Classroom.art_teacher_id,
            )
            .filter(
                Classroom.school_year == item.target_school_year,
                Classroom.semester == item.target_semester,
            )
            .all()
        )
        for head_id, assistant_id, art_id in target_teacher_rows:
            for teacher_id in (head_id, assistant_id, art_id):
                if teacher_id:
                    affected_teacher_ids.add(teacher_id)
        session.flush()
        _sync_employee_classroom_id(session, sorted(affected_teacher_ids))

        # audit：整批升班動作摘要（逐人 StudentChangeLog 已留痕，這層為整體軌跡，
        # 對齊 bulk-transfer 的 request.state.audit_changes pattern）。
        if request is not None:
            request.state.audit_changes = {
                "action": "promote_academic_year",
                "source_term": _semester_label(
                    item.source_school_year, item.source_semester
                ),
                "target_term": _semester_label(
                    item.target_school_year, item.target_semester
                ),
                "created_count": created_count,
                "moved_student_count": moved_student_count,
                "graduated_count": graduated_count,
            }

        session.commit()
        logger.info(
            "班級跨學年升班成功 source=%s-%s target=%s-%s created=%s moved_students=%s graduated=%s operator=%s",
            item.source_school_year,
            item.source_semester,
            item.target_school_year,
            item.target_semester,
            created_count,
            moved_student_count,
            graduated_count,
            current_user.get("username"),
        )
        return {
            "message": "升班完成",
            "created_count": created_count,
            "moved_student_count": moved_student_count,
            "graduated_count": graduated_count,
            "target_term": _semester_label(
                item.target_school_year, item.target_semester
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級跨學年升班失敗")
        raise_safe_500(e, context="升班失敗")
    finally:
        session.close()


@router.post(
    "/classrooms/promote-academic-year/preview",
    response_model=ClassroomPromotePreviewOut,
)
def preview_promote_classrooms_to_academic_year(
    item: ClassroomPromoteAcademicYear,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """跨學年升班試算（不寫入）：回傳逐班處置、彙總與阻擋性衝突清單。

    供前端「預覽 + 確認」流程使用；與 execute 共用 _build_promotion_plan 確保
    數字與衝突判定一致。整批層級錯誤（來源=目標學期相同）仍 raise 400，逐班
    衝突收進 conflicts 回 200。
    """
    session = get_session()
    try:
        if (
            item.source_school_year == item.target_school_year
            and item.source_semester == item.target_semester
        ):
            raise HTTPException(status_code=400, detail="來源學期與目標學期不可相同")

        plan = _build_promotion_plan(session, item)

        rows = [
            ClassroomPromotePreviewRowOut(
                source_classroom_id=prep.source.id,
                source_name=prep.source.name,
                source_grade_id=prep.source.grade_id,
                source_grade_name=prep.source_grade_name,
                resolved_target_grade_id=prep.resolved_grade_id,
                resolved_target_grade_name=prep.resolved_grade_name,
                target_name=None if prep.will_graduate else prep.item.target_name,
                will_graduate=prep.will_graduate,
                active_student_count=prep.active_student_count,
                reuses_existing_target=prep.reuses_existing_target,
            )
            for prep in plan.prepared_rows
        ]
        conflicts = [
            ClassroomPromoteConflictOut(
                kind=c.kind,
                source_classroom_id=c.source_classroom_id,
                target_name=c.target_name,
                message=c.message,
            )
            for c in plan.conflicts
        ]
        return ClassroomPromotePreviewOut(
            source_term=_semester_label(item.source_school_year, item.source_semester),
            target_term=_semester_label(item.target_school_year, item.target_semester),
            rows=rows,
            will_create_count=plan.will_create_count,
            will_move_student_count=plan.will_move_student_count,
            will_graduate_count=plan.will_graduate_count,
            conflicts=conflicts,
            has_blocking_conflict=bool(plan.conflicts),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("班級跨學年升班預覽失敗")
        raise_safe_500(e, context="升班預覽失敗")
    finally:
        session.close()


@router.delete("/classrooms/{classroom_id}", response_model=MutationResultOut)
def delete_classroom(
    classroom_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """停用班級。若仍有在學學生，則拒絕停用。"""
    session = get_session()
    try:
        classroom = (
            session.query(Classroom).filter(Classroom.id == classroom_id).first()
        )
        if not classroom:
            raise HTTPException(status_code=404, detail=CLASSROOM_NOT_FOUND)

        active_student_count = (
            session.query(func.count(Student.id))
            .filter(
                Student.classroom_id == classroom.id,
                Student.is_active == True,
            )
            .scalar()
            or 0
        )
        if active_student_count > 0:
            raise HTTPException(
                status_code=409, detail="班級仍有在學學生，請先轉班或移出學生後再停用"
            )

        affected_teacher_ids = _classroom_teacher_ids(classroom)
        classroom.is_active = False
        classroom.head_teacher_id = None
        classroom.assistant_teacher_id = None
        classroom.art_teacher_id = None
        session.flush()
        _sync_employee_classroom_id(session, affected_teacher_ids)
        session.commit()
        return {"message": "班級已停用", "id": classroom.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級停用失敗 classroom_id=%s", classroom_id)
        raise_safe_500(e, context="停用失敗")
    finally:
        session.close()


@router.get("/grades", response_model=list[GradeOut])
def get_grades(
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_READ)),
):
    """取得所有年級"""
    session = get_session()
    try:
        grades = (
            session.query(ClassGrade)
            .filter(ClassGrade.is_active == True)
            .order_by(ClassGrade.sort_order.desc())
            .all()
        )
        return [
            {
                "id": g.id,
                "name": g.name,
                "age_range": g.age_range,
                "sort_order": g.sort_order,
                "is_graduation_grade": bool(g.is_graduation_grade),
            }
            for g in grades
        ]
    finally:
        session.close()


class GradeUpdate(BaseModel):
    is_graduation_grade: Optional[bool] = None


@router.patch("/grades/{grade_id}", response_model=GradeOut)
def update_grade(
    grade_id: int,
    item: GradeUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """更新年級設定（目前僅支援切換是否為畢業班年級）"""
    session = get_session()
    try:
        grade = session.query(ClassGrade).filter(ClassGrade.id == grade_id).first()
        if not grade:
            raise HTTPException(status_code=404, detail="找不到該年級")

        patch = item.model_dump(exclude_unset=True)
        if "is_graduation_grade" in patch:
            grade.is_graduation_grade = bool(patch["is_graduation_grade"])

        session.commit()
        return {
            "id": grade.id,
            "name": grade.name,
            "is_graduation_grade": bool(grade.is_graduation_grade),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("更新年級失敗 grade_id=%s", grade_id)
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()
