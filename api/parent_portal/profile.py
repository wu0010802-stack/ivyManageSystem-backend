"""api/parent_portal/profile.py — 家長端個人資料與子女清單。

- GET /api/parent/me：個人資料 + 推播可達性
- GET /api/parent/my-children：所有監護的學生清單（含班級名稱、目前 lifecycle_status）
"""

from fastapi import APIRouter, Depends

from models.database import Classroom, Guardian, Student, get_session
from utils.auth import require_parent_role

from ._shared import _get_parent_user

router = APIRouter(tags=["parent-profile"])


@router.get("/me")
def get_me(current_user: dict = Depends(require_parent_role())):
    session = get_session()
    try:
        user = _get_parent_user(session, current_user)
        return {
            "user_id": user.id,
            "name": user.username,
            "line_user_id": user.line_user_id,
            "role": "parent",
            "can_push": user.line_follow_confirmed_at is not None,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        }
    finally:
        session.close()


@router.get("/my-children")
def get_my_children(current_user: dict = Depends(require_parent_role())):
    """回傳家長監護的所有活的學生（依 enrollment_date 排序）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        rows = (
            session.query(Guardian, Student, Classroom)
            .join(Student, Student.id == Guardian.student_id)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(
                Guardian.user_id == user_id,
                Guardian.deleted_at.is_(None),
            )
            .order_by(Student.enrollment_date.asc().nulls_last(), Student.name.asc())
            .all()
        )
        children = []
        for guardian, student, classroom in rows:
            children.append(
                {
                    "guardian_id": guardian.id,
                    "guardian_relation": guardian.relation,
                    "is_primary": bool(guardian.is_primary),
                    "can_pickup": bool(guardian.can_pickup),
                    "student_id": student.id,
                    "student_no": student.student_id,
                    "name": student.name,
                    "gender": student.gender,
                    "birthday": student.birthday.isoformat() if student.birthday else None,
                    "classroom_id": classroom.id if classroom else None,
                    "classroom_name": classroom.name if classroom else None,
                    "lifecycle_status": student.lifecycle_status,
                    "is_active": bool(student.is_active),
                }
            )
        return {"items": children, "total": len(children)}
    finally:
        session.close()
