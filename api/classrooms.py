"""
Classroom management router
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from models.database import get_session, Classroom, ClassGrade, Employee, Student

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["classrooms"])


# ============ Routes ============

@router.get("/classrooms")
async def get_classrooms():
    """取得所有班級列表（含老師和學生數）"""
    session = get_session()
    try:
        # 依班級 ID 排序，確保順序一致
        classrooms = session.query(Classroom).filter(Classroom.is_active == True).order_by(Classroom.id).all()
        result = []
        for c in classrooms:
            # 取得年級名稱
            grade = session.query(ClassGrade).filter(ClassGrade.id == c.grade_id).first() if c.grade_id else None

            # 取得班導師
            head_teacher = session.query(Employee).filter(Employee.id == c.head_teacher_id).first() if c.head_teacher_id else None

            # 取得副班導
            assistant_teacher = session.query(Employee).filter(Employee.id == c.assistant_teacher_id).first() if c.assistant_teacher_id else None

            # 取得學生數
            student_count = session.query(Student).filter(
                Student.classroom_id == c.id,
                Student.is_active == True
            ).count()

            # 取得美師
            art_teacher = session.query(Employee).filter(Employee.id == c.art_teacher_id).first() if c.art_teacher_id else None

            result.append({
                "id": c.id,
                "name": c.name,
                "class_code": c.class_code,
                "grade_id": c.grade_id,
                "grade_name": grade.name if grade else None,
                "capacity": c.capacity,
                "current_count": student_count,
                "head_teacher_id": c.head_teacher_id,
                "head_teacher_name": head_teacher.name if head_teacher else None,
                "assistant_teacher_id": c.assistant_teacher_id,
                "assistant_teacher_name": assistant_teacher.name if assistant_teacher else None,
                "art_teacher_id": c.art_teacher_id,
                "art_teacher_name": art_teacher.name if art_teacher else None,
                "is_active": c.is_active
            })
        return result
    finally:
        session.close()


@router.get("/classrooms/{classroom_id}")
async def get_classroom(classroom_id: int):
    """取得單一班級詳細資料（含學生列表）"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        # 取得年級
        grade = session.query(ClassGrade).filter(ClassGrade.id == classroom.grade_id).first() if classroom.grade_id else None

        # 取得老師
        head_teacher = session.query(Employee).filter(Employee.id == classroom.head_teacher_id).first() if classroom.head_teacher_id else None
        assistant_teacher = session.query(Employee).filter(Employee.id == classroom.assistant_teacher_id).first() if classroom.assistant_teacher_id else None

        # 取得學生列表
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
            "grade_name": grade.name if grade else None,
            "capacity": classroom.capacity,
            "current_count": len(student_list),
            "head_teacher_id": classroom.head_teacher_id,
            "head_teacher_name": head_teacher.name if head_teacher else None,
            "assistant_teacher_id": classroom.assistant_teacher_id,
            "assistant_teacher_name": assistant_teacher.name if assistant_teacher else None,
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
    art_teacher_id: Optional[int] = None
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
async def get_grades():
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
