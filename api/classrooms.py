"""
Classroom management router
"""

import logging
from typing import Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from utils.errors import raise_safe_500
from pydantic import BaseModel, Field, field_validator, model_validator

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from models.database import get_session, Classroom, ClassGrade, Employee, Student
from utils.academic import resolve_current_academic_term, resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.error_messages import CLASSROOM_NOT_FOUND
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["classrooms"])


# ============ Pydantic Models ============


class ClassroomCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    class_code: Optional[str] = Field(None, max_length=20)
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)
    grade_id: Optional[int] = Field(None, ge=1)
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


def _serialize_classroom_detail(session, classroom: Classroom):
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

    student_list = [
        {
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
        for s in students
    ]

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


@router.get("/classrooms")
async def get_classrooms(
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
        return result
    finally:
        session.close()


@router.get("/classrooms/teacher-options")
async def get_teacher_options(
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


@router.get("/classrooms/{classroom_id}")
async def get_classroom(
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
        return _serialize_classroom_detail(session, classroom)
    finally:
        session.close()


@router.get("/classrooms/{classroom_id}/enrollment-composition")
async def get_classroom_enrollment_composition(
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
            "snapshot_date": date.today().isoformat(),
            "total": total,
            "counts": counts,
            "ratios": {
                k: round(v / total, 4) if total else 0.0 for k, v in counts.items()
            },
            "timeline": [],  # 留給 v2
        }
    finally:
        session.close()


@router.post("/classrooms", status_code=201)
async def create_classroom(
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


@router.put("/classrooms/{classroom_id}")
async def update_classroom(
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


@router.post("/classrooms/clone-term", status_code=201)
async def clone_classrooms_to_term(
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


@router.post("/classrooms/promote-academic-year", status_code=201)
async def promote_classrooms_to_academic_year(
    item: ClassroomPromoteAcademicYear,
    current_user: dict = Depends(require_staff_permission(Permission.CLASSROOMS_WRITE)),
):
    """跨學年升班：建立新班、沿用老師並搬移在讀學生。"""
    session = get_session()
    try:
        if (
            item.source_school_year == item.target_school_year
            and item.source_semester == item.target_semester
        ):
            raise HTTPException(status_code=400, detail="來源學期與目標學期不可相同")

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
        missing_source_ids = [
            classroom_id
            for classroom_id in source_ids
            if classroom_id not in source_map
        ]
        if missing_source_ids:
            raise HTTPException(
                status_code=404, detail=f"找不到來源班級：{missing_source_ids}"
            )

        grade_map = _get_grade_map(session)
        prepared_rows = []
        for row in item.classrooms:
            source = source_map[row.source_classroom_id]
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
                raise HTTPException(
                    status_code=400, detail=f"班級「{source.name}」缺少新班名"
                )
            prepared_rows.append((row, source, resolved_grade_id, will_graduate))

        target_names = [
            row.target_name
            for row, _, _, will_graduate in prepared_rows
            if not will_graduate and row.target_name
        ]
        if len(target_names) != len(set(target_names)):
            raise HTTPException(status_code=409, detail="目標學期的班級名稱不可重複")

        existing_targets = []
        if target_names:
            existing_targets = (
                session.query(Classroom)
                .filter(
                    Classroom.school_year == item.target_school_year,
                    Classroom.semester == item.target_semester,
                    func.lower(Classroom.name).in_(
                        [name.lower() for name in target_names]
                    ),
                )
                .all()
            )
        active_conflicts = [row for row in existing_targets if row.is_active]
        reusable_targets = {
            row.name.lower(): row for row in existing_targets if not row.is_active
        }
        if active_conflicts:
            conflicted_names = "、".join(sorted({row.name for row in active_conflicts}))
            raise HTTPException(
                status_code=409, detail=f"目標學期已存在相同班級：{conflicted_names}"
            )

        created_count = 0
        moved_student_count = 0
        graduated_count = 0
        graduation_date = _term_start_date(
            item.target_school_year, item.target_semester
        )

        for row, source, target_grade_id, will_graduate in prepared_rows:
            if will_graduate:
                graduated = (
                    session.query(Student)
                    .filter(
                        Student.classroom_id == source.id,
                        Student.is_active == True,
                    )
                    .update(
                        {
                            Student.is_active: False,
                            Student.status: "已畢業",
                            Student.graduation_date: graduation_date,
                        },
                        synchronize_session=False,
                    )
                )
                graduated_count += graduated or 0
                continue

            if target_grade_id not in grade_map:
                raise HTTPException(
                    status_code=400, detail="指定的目標年級不存在或已停用"
                )

            reusable_target = reusable_targets.pop(row.target_name.lower(), None)
            if reusable_target:
                existing_active_student_count = (
                    session.query(func.count(Student.id))
                    .filter(
                        Student.classroom_id == reusable_target.id,
                        Student.is_active == True,
                    )
                    .scalar()
                    or 0
                )
                if existing_active_student_count > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=f"目標班級「{reusable_target.name}」仍有在讀學生，無法直接重用",
                    )
                reusable_target.class_code = source.class_code
                reusable_target.grade_id = target_grade_id
                reusable_target.capacity = source.capacity
                reusable_target.head_teacher_id = (
                    source.head_teacher_id if row.copy_teachers else None
                )
                reusable_target.assistant_teacher_id = (
                    source.assistant_teacher_id if row.copy_teachers else None
                )
                reusable_target.art_teacher_id = (
                    source.art_teacher_id if row.copy_teachers else None
                )
                reusable_target.is_active = True
                target_classroom = reusable_target
            else:
                target_classroom = Classroom(
                    name=row.target_name,
                    class_code=source.class_code,
                    school_year=item.target_school_year,
                    semester=item.target_semester,
                    grade_id=target_grade_id,
                    capacity=source.capacity,
                    head_teacher_id=(
                        source.head_teacher_id if row.copy_teachers else None
                    ),
                    assistant_teacher_id=(
                        source.assistant_teacher_id if row.copy_teachers else None
                    ),
                    art_teacher_id=source.art_teacher_id if row.copy_teachers else None,
                    is_active=True,
                )
                session.add(target_classroom)
                session.flush()
            created_count += 1

            if row.move_students:
                moved = (
                    session.query(Student)
                    .filter(
                        Student.classroom_id == source.id,
                        Student.is_active == True,
                    )
                    .update(
                        {Student.classroom_id: target_classroom.id},
                        synchronize_session=False,
                    )
                )
                moved_student_count += moved or 0

        # 升班完成後同步所有受影響教師的 Employee.classroom_id：
        # 把來源班教師（取消指派）與目標班教師（新指派）一併重算。
        affected_teacher_ids: set[int] = set()
        for row, source, _, will_graduate in prepared_rows:
            affected_teacher_ids.update(_classroom_teacher_ids(source))
            if will_graduate:
                continue
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


@router.delete("/classrooms/{classroom_id}")
async def delete_classroom(
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


@router.get("/grades")
async def get_grades(
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


@router.patch("/grades/{grade_id}")
async def update_grade(
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
