"""api/parent_portal/profile.py — 家長端個人資料與子女清單。

- GET /api/parent/me：個人資料 + 推播可達性
- GET /api/parent/my-children：所有監護的學生清單（含班級名稱、目前 lifecycle_status）
- GET /api/parent/students/{student_id}/profile：單一子女完整檔案（過敏 / 接送家長 /
  老師 / 用藥概況）— 提供 ChildProfileView 使用，純讀取，修改走訊息給導師。
"""

from fastapi import APIRouter, Depends, HTTPException

from models.database import (
    Classroom,
    Employee,
    Guardian,
    Student,
    get_session,
)
from models.portfolio import StudentAllergy
from utils.auth import require_parent_role

from ._shared import (
    _assert_student_owned,
    _get_parent_user,
    resolve_parent_display_name,
)

router = APIRouter(tags=["parent-profile"])


@router.get("/me")
def get_me(current_user: dict = Depends(require_parent_role())):
    session = get_session()
    try:
        user = _get_parent_user(session, current_user)
        return {
            "user_id": user.id,
            "name": resolve_parent_display_name(session, user),
            "line_user_id": user.line_user_id,
            "role": "parent",
            "can_push": user.line_follow_confirmed_at is not None,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        }
    finally:
        session.close()


@router.get("/students/{student_id}/profile")
def get_child_profile(
    student_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """單一子女完整檔案：班級 / 老師 / 監護人清單（與本家長同 student）/ 過敏。

    家長端唯讀；如需修改：前端引導使用者開啟訊息給導師（避免直接寫 DB
    引發稽核軌跡與正確性問題）。
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="學生不存在")
        classroom = (
            session.query(Classroom)
            .filter(Classroom.id == student.classroom_id)
            .first()
            if student.classroom_id
            else None
        )

        # 老師（班導 / 副班導 / 美術老師）— 對外只露 name
        teacher_ids = []
        if classroom:
            for tid in (
                classroom.head_teacher_id,
                classroom.assistant_teacher_id,
                classroom.art_teacher_id,
            ):
                if tid:
                    teacher_ids.append(tid)
        teacher_map = {}
        if teacher_ids:
            for emp in (
                session.query(Employee).filter(Employee.id.in_(teacher_ids)).all()
            ):
                teacher_map[emp.id] = emp.name
        teachers = []
        if classroom:
            if classroom.head_teacher_id:
                teachers.append(
                    {
                        "role": "head",
                        "label": "班導",
                        "name": teacher_map.get(classroom.head_teacher_id),
                    }
                )
            if classroom.assistant_teacher_id:
                teachers.append(
                    {
                        "role": "assistant",
                        "label": "副班導",
                        "name": teacher_map.get(classroom.assistant_teacher_id),
                    }
                )
            if classroom.art_teacher_id:
                teachers.append(
                    {
                        "role": "art",
                        "label": "美術老師",
                        "name": teacher_map.get(classroom.art_teacher_id),
                    }
                )

        # 同一 student 的所有監護人（含其他家長），不揭露 user_id / line_user_id
        guardians = []
        for g in (
            session.query(Guardian)
            .filter(
                Guardian.student_id == student_id,
                Guardian.deleted_at.is_(None),
            )
            .order_by(Guardian.is_primary.desc(), Guardian.id.asc())
            .all()
        ):
            guardians.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "relation": g.relation,
                    "is_primary": bool(g.is_primary),
                    "can_pickup": bool(g.can_pickup),
                    "is_self": g.user_id == user_id,
                }
            )

        # 過敏（active）
        allergies = [
            {
                "id": a.id,
                "allergen": a.allergen,
                "severity": a.severity,
                "reaction_symptom": a.reaction_symptom,
                "first_aid_note": a.first_aid_note,
            }
            for a in session.query(StudentAllergy)
            .filter(
                StudentAllergy.student_id == student_id,
                StudentAllergy.active == True,  # noqa: E712
            )
            .order_by(StudentAllergy.severity.desc(), StudentAllergy.id.asc())
            .all()
        ]

        return {
            "student": {
                "id": student.id,
                "student_no": student.student_id,
                "name": student.name,
                "gender": student.gender,
                "birthday": (
                    student.birthday.isoformat() if student.birthday else None
                ),
                "lifecycle_status": student.lifecycle_status,
            },
            "classroom": (
                {"id": classroom.id, "name": classroom.name} if classroom else None
            ),
            "teachers": teachers,
            "guardians": guardians,
            "allergies": allergies,
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
                    "birthday": (
                        student.birthday.isoformat() if student.birthday else None
                    ),
                    "classroom_id": classroom.id if classroom else None,
                    "classroom_name": classroom.name if classroom else None,
                    "lifecycle_status": student.lifecycle_status,
                    "is_active": bool(student.is_active),
                }
            )
        return {"items": children, "total": len(children)}
    finally:
        session.close()
