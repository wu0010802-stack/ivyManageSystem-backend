"""
Classroom management router
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from sqlalchemy import func
from models.database import get_session, Classroom, ClassGrade, Employee, Student
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["classrooms"])


# ============ Pydantic Models ============

class ClassroomCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    class_code: Optional[str] = Field(None, max_length=20)
    grade_id: Optional[int] = Field(None, ge=1)
    capacity: int = Field(30, ge=1, le=200)
    head_teacher_id: Optional[int] = Field(None, ge=1)
    assistant_teacher_id: Optional[int] = Field(None, ge=1)
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


class ClassroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    class_code: Optional[str] = Field(None, max_length=20)
    grade_id: Optional[int] = Field(None, ge=1)
    capacity: Optional[int] = Field(None, ge=1, le=200)
    head_teacher_id: Optional[int] = Field(None, ge=1)
    assistant_teacher_id: Optional[int] = Field(None, ge=1)
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


# ============ Helpers ============

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
        raise HTTPException(status_code=400, detail="同一位老師不可同時擔任同班多個角色")


def _validate_grade_exists(session, grade_id: Optional[int]):
    if grade_id is None:
        return
    grade = session.query(ClassGrade.id).filter(
        ClassGrade.id == grade_id,
        ClassGrade.is_active == True,
    ).first()
    if not grade:
        raise HTTPException(status_code=400, detail="指定的年級不存在或已停用")


def _validate_teacher_ids(session, teacher_ids: list[int]):
    if not teacher_ids:
        return
    teachers = session.query(Employee.id).filter(
        Employee.id.in_(teacher_ids),
        Employee.is_active == True,
    ).all()
    existing_ids = {teacher.id for teacher in teachers}
    missing_ids = [teacher_id for teacher_id in teacher_ids if teacher_id not in existing_ids]
    if missing_ids:
        raise HTTPException(status_code=400, detail=f"指定的教師不存在或已停用: {missing_ids}")


def _validate_unique_classroom(session, name: Optional[str], class_code: Optional[str], classroom_id: Optional[int] = None):
    if name:
        q = session.query(Classroom.id).filter(func.lower(Classroom.name) == name.lower())
        if classroom_id is not None:
            q = q.filter(Classroom.id != classroom_id)
        if q.first():
            raise HTTPException(status_code=400, detail="班級名稱已存在")

    if class_code:
        q = session.query(Classroom.id).filter(func.lower(Classroom.class_code) == class_code.lower())
        if classroom_id is not None:
            q = q.filter(Classroom.id != classroom_id)
        if q.first():
            raise HTTPException(status_code=400, detail="班級代號已存在")


def _serialize_classroom_detail(session, classroom: Classroom):
    grade_name = None
    if classroom.grade_id:
        grade = session.query(ClassGrade).filter(ClassGrade.id == classroom.grade_id).first()
        grade_name = grade.name if grade else None

    teacher_ids = [
        tid
        for tid in (classroom.head_teacher_id, classroom.assistant_teacher_id, classroom.art_teacher_id)
        if tid
    ]
    teacher_map = {}
    if teacher_ids:
        teachers = session.query(Employee.id, Employee.name).filter(Employee.id.in_(teacher_ids)).all()
        teacher_map = {t.id: t.name for t in teachers}

    students = session.query(Student).filter(
        Student.classroom_id == classroom.id,
        Student.is_active == True
    ).order_by(Student.name).all()

    student_list = [{
        "id": s.id,
        "student_id": s.student_id,
        "name": s.name,
        "gender": s.gender
    } for s in students]

    return {
        "id": classroom.id,
        "name": classroom.name,
        "class_code": classroom.class_code,
        "grade_id": classroom.grade_id,
        "grade_name": grade_name,
        "capacity": classroom.capacity,
        "current_count": len(student_list),
        "head_teacher_id": classroom.head_teacher_id,
        "head_teacher_name": teacher_map.get(classroom.head_teacher_id),
        "assistant_teacher_id": classroom.assistant_teacher_id,
        "assistant_teacher_name": teacher_map.get(classroom.assistant_teacher_id),
        "art_teacher_id": classroom.art_teacher_id,
        "art_teacher_name": teacher_map.get(classroom.art_teacher_id),
        "students": student_list,
        "is_active": classroom.is_active
    }


# ============ Routes ============

@router.get("/classrooms")
async def get_classrooms(
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ)),
):
    """取得所有班級列表（含老師和學生數）"""
    session = get_session()
    try:
        q = session.query(Classroom)
        if not include_inactive:
            q = q.filter(Classroom.is_active == True)
        classrooms = q.order_by(Classroom.is_active.desc(), Classroom.id).all()
        if not classrooms:
            return []

        # 批量載入年級
        grade_ids = {c.grade_id for c in classrooms if c.grade_id}
        grade_map = {}
        if grade_ids:
            grades = session.query(ClassGrade).filter(ClassGrade.id.in_(grade_ids)).all()
            grade_map = {g.id: g.name for g in grades}

        # 批量載入老師
        teacher_ids = set()
        for c in classrooms:
            for tid in (c.head_teacher_id, c.assistant_teacher_id, c.art_teacher_id):
                if tid:
                    teacher_ids.add(tid)
        teacher_map = {}
        if teacher_ids:
            teachers = session.query(Employee.id, Employee.name).filter(Employee.id.in_(teacher_ids)).all()
            teacher_map = {t.id: t.name for t in teachers}

        # 批量取得各班學生數（單一聚合查詢）
        classroom_ids = [c.id for c in classrooms]
        student_counts = session.query(
            Student.classroom_id, func.count(Student.id)
        ).filter(
            Student.classroom_id.in_(classroom_ids),
            Student.is_active == True
        ).group_by(Student.classroom_id).all()
        count_map = dict(student_counts)

        result = []
        for c in classrooms:
            result.append({
                "id": c.id,
                "name": c.name,
                "class_code": c.class_code,
                "grade_id": c.grade_id,
                "grade_name": grade_map.get(c.grade_id),
                "capacity": c.capacity,
                "current_count": count_map.get(c.id, 0),
                "head_teacher_id": c.head_teacher_id,
                "head_teacher_name": teacher_map.get(c.head_teacher_id),
                "assistant_teacher_id": c.assistant_teacher_id,
                "assistant_teacher_name": teacher_map.get(c.assistant_teacher_id),
                "art_teacher_id": c.art_teacher_id,
                "art_teacher_name": teacher_map.get(c.art_teacher_id),
                "is_active": c.is_active
            })
        return result
    finally:
        session.close()


@router.get("/classrooms/teacher-options")
async def get_teacher_options(current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ))):
    """取得可指派教師清單。"""
    session = get_session()
    try:
        teachers = session.query(Employee.id, Employee.name).filter(
            Employee.is_active == True
        ).order_by(Employee.name).all()
        return [{"id": teacher.id, "name": teacher.name} for teacher in teachers]
    finally:
        session.close()


@router.get("/classrooms/{classroom_id}")
async def get_classroom(classroom_id: int, current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ))):
    """取得單一班級詳細資料（含學生列表）"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")
        return _serialize_classroom_detail(session, classroom)
    finally:
        session.close()


@router.post("/classrooms", status_code=201)
async def create_classroom(
    item: ClassroomCreate,
    current_user: dict = Depends(require_permission(Permission.CLASSROOMS_WRITE)),
):
    """新增班級"""
    session = get_session()
    try:
        _validate_distinct_teacher_assignments(
            item.head_teacher_id,
            item.assistant_teacher_id,
            item.art_teacher_id,
        )
        _validate_grade_exists(session, item.grade_id)
        _validate_teacher_ids(
            session,
            [
                teacher_id for teacher_id in (
                    item.head_teacher_id,
                    item.assistant_teacher_id,
                    item.art_teacher_id,
                ) if teacher_id is not None
            ],
        )
        _validate_unique_classroom(session, item.name, item.class_code)

        classroom = Classroom(**item.model_dump())
        session.add(classroom)
        session.commit()
        return {"message": "班級新增成功", "id": classroom.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級新增失敗")
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@router.put("/classrooms/{classroom_id}")
async def update_classroom(
    classroom_id: int,
    item: ClassroomUpdate,
    current_user: dict = Depends(require_permission(Permission.CLASSROOMS_WRITE)),
):
    """更新班級資料"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        update_data = item.model_dump(exclude_unset=True)

        head_teacher_id = update_data.get("head_teacher_id", classroom.head_teacher_id)
        assistant_teacher_id = update_data.get("assistant_teacher_id", classroom.assistant_teacher_id)
        art_teacher_id = update_data.get("art_teacher_id", classroom.art_teacher_id)
        _validate_distinct_teacher_assignments(head_teacher_id, assistant_teacher_id, art_teacher_id)

        if "grade_id" in update_data:
            _validate_grade_exists(session, update_data["grade_id"])

        _validate_teacher_ids(
            session,
            [
                teacher_id for teacher_id in (
                    head_teacher_id,
                    assistant_teacher_id,
                    art_teacher_id,
                ) if teacher_id is not None
            ],
        )

        _validate_unique_classroom(
            session,
            update_data.get("name"),
            update_data.get("class_code"),
            classroom_id=classroom.id,
        )

        NULLABLE_FIELDS = {"grade_id", "head_teacher_id", "assistant_teacher_id", "art_teacher_id", "class_code"}
        for key, value in update_data.items():
            if value is not None or key in NULLABLE_FIELDS:
                setattr(classroom, key, value)

        session.commit()
        return {"message": "班級更新成功", "id": classroom.id, "name": classroom.name}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級更新失敗 classroom_id=%s", classroom_id)
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@router.delete("/classrooms/{classroom_id}")
async def delete_classroom(
    classroom_id: int,
    current_user: dict = Depends(require_permission(Permission.CLASSROOMS_WRITE)),
):
    """停用班級。若仍有在學學生，則拒絕停用。"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        active_student_count = session.query(func.count(Student.id)).filter(
            Student.classroom_id == classroom.id,
            Student.is_active == True,
        ).scalar() or 0
        if active_student_count > 0:
            raise HTTPException(status_code=409, detail="班級仍有在學學生，請先轉班或移出學生後再停用")

        classroom.is_active = False
        classroom.head_teacher_id = None
        classroom.assistant_teacher_id = None
        classroom.art_teacher_id = None
        session.commit()
        return {"message": "班級已停用", "id": classroom.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("班級停用失敗 classroom_id=%s", classroom_id)
        raise HTTPException(status_code=500, detail=f"停用失敗: {str(e)}")
    finally:
        session.close()


@router.get("/grades")
async def get_grades(current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ))):
    """取得所有年級"""
    session = get_session()
    try:
        grades = session.query(ClassGrade).filter(ClassGrade.is_active == True).order_by(ClassGrade.sort_order.desc()).all()
        return [{
            "id": g.id,
            "name": g.name,
            "age_range": g.age_range
        } for g in grades]
    finally:
        session.close()
