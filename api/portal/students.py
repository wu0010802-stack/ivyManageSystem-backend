"""
Portal - my students endpoint
"""

from collections import defaultdict
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

        if not classrooms:
            return {
                "employee_name": emp.name,
                "classrooms": [],
                "total_students": 0,
            }

        # 單次 IN 查詢取得所有班級的學生，再按 classroom_id 分組
        classroom_ids = [cr.id for cr in classrooms]
        all_students = (
            session.query(Student)
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active == True,
            )
            .order_by(Student.name)
            .all()
        )
        students_by_classroom = defaultdict(list)
        for s in all_students:
            students_by_classroom[s.classroom_id].append(s)

        result = []
        for cr in classrooms:
            role = "教師"
            if cr.head_teacher_id == emp.id:
                role = "主教老師"
            elif cr.assistant_teacher_id == emp.id:
                role = "助教老師"
            elif cr.art_teacher_id == emp.id:
                role = "美語老師"

            students = students_by_classroom[cr.id]

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
