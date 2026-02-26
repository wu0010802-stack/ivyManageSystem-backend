"""
Classroom management router
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import func
from models.database import get_session, Classroom, ClassGrade, Employee, Student
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["classrooms"])


# ============ Routes ============

@router.get("/classrooms")
async def get_classrooms(current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ))):
    """取得所有班級列表（含老師和學生數）"""
    session = get_session()
    try:
        classrooms = session.query(Classroom).filter(Classroom.is_active == True).order_by(Classroom.id).all()
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


@router.get("/classrooms/{classroom_id}")
async def get_classroom(classroom_id: int, current_user: dict = Depends(require_permission(Permission.CLASSROOMS_READ))):
    """取得單一班級詳細資料（含學生列表）"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        # 批量載入年級和老師（最多 1+1 條查詢）
        grade_name = None
        if classroom.grade_id:
            grade = session.query(ClassGrade).filter(ClassGrade.id == classroom.grade_id).first()
            grade_name = grade.name if grade else None

        teacher_ids = [tid for tid in (classroom.head_teacher_id, classroom.assistant_teacher_id) if tid]
        teacher_map = {}
        if teacher_ids:
            teachers = session.query(Employee.id, Employee.name).filter(Employee.id.in_(teacher_ids)).all()
            teacher_map = {t.id: t.name for t in teachers}

        students = session.query(Student).filter(
            Student.classroom_id == classroom_id,
            Student.is_active == True
        ).all()

        student_list = [{
            "id": s.id,
            "student_id": s.student_id,
            "name": s.name,
            "gender": s.gender
        } for s in students]

        return {
            "id": classroom.id,
            "name": classroom.name,
            "grade_id": classroom.grade_id,
            "grade_name": grade_name,
            "capacity": classroom.capacity,
            "current_count": len(student_list),
            "head_teacher_id": classroom.head_teacher_id,
            "head_teacher_name": teacher_map.get(classroom.head_teacher_id),
            "assistant_teacher_id": classroom.assistant_teacher_id,
            "assistant_teacher_name": teacher_map.get(classroom.assistant_teacher_id),
            "students": student_list,
            "is_active": classroom.is_active
        }
    finally:
        session.close()


@router.put("/classrooms/{classroom_id}")
async def update_classroom(
    classroom_id: int,
    head_teacher_id: Optional[int] = None,
    assistant_teacher_id: Optional[int] = None,
    art_teacher_id: Optional[int] = None,
    current_user: dict = Depends(require_permission(Permission.CLASSROOMS_WRITE)),
):
    """更新班級老師"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        if head_teacher_id is not None:
            classroom.head_teacher_id = head_teacher_id if head_teacher_id > 0 else None
        if assistant_teacher_id is not None:
            classroom.assistant_teacher_id = assistant_teacher_id if assistant_teacher_id > 0 else None
        if art_teacher_id is not None:
            classroom.art_teacher_id = art_teacher_id if art_teacher_id > 0 else None

        session.commit()
        return {"message": "班級更新成功", "id": classroom.id, "name": classroom.name}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
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
