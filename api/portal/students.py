"""
Portal - my students endpoint
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_

from models.database import get_session, Classroom, Student
from utils.auth import get_current_user
from ._shared import _get_employee

router = APIRouter()


@router.get("/my-students")
def get_my_students(
    classroom_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """取得教師所屬班級的學生資料"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        query = session.query(Classroom).filter(
            Classroom.is_active == True,
            or_(
                Classroom.head_teacher_id == emp.id,
                Classroom.assistant_teacher_id == emp.id,
                Classroom.art_teacher_id == emp.id,
            ),
        )
        if classroom_id:
            query = query.filter(Classroom.id == classroom_id)

        classrooms = query.all()

        result = []
        for cr in classrooms:
            role = "教師"
            if cr.head_teacher_id == emp.id:
                role = "主教老師"
            elif cr.assistant_teacher_id == emp.id:
                role = "助教老師"
            elif cr.art_teacher_id == emp.id:
                role = "美術老師"

            students = session.query(Student).filter(
                Student.classroom_id == cr.id,
                Student.is_active == True,
            ).order_by(Student.name).all()

            result.append({
                "classroom_id": cr.id,
                "classroom_name": cr.name,
                "role": role,
                "student_count": len(students),
                "students": [{
                    "id": s.id,
                    "student_id": s.student_id,
                    "name": s.name,
                    "gender": s.gender,
                    "birthday": s.birthday.isoformat() if s.birthday else None,
                    "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
                    "parent_name": s.parent_name,
                    "parent_phone": s.parent_phone,
                    "address": s.address,
                    "status_tag": s.status_tag,
                    "notes": s.notes,
                } for s in students],
            })

        return {
            "employee_name": emp.name,
            "classrooms": result,
            "total_students": sum(c["student_count"] for c in result),
        }
    finally:
        session.close()
