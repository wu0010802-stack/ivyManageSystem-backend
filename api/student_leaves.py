"""api/student_leaves.py — 教師端唯讀清單。

家長端提交即自動核准（見 api/parent_portal/leaves.py），教師端不再進行 approve/reject。
保留此 router 用於：
- GET /api/student-leaves：列出班級 scope 內的請假紀錄（預設 status=approved）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import Student, StudentLeaveRequest, get_session
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import accessible_classroom_ids, is_unrestricted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/student-leaves", tags=["student-leaves"])


def _serialize(item: StudentLeaveRequest, student_name: Optional[str] = None) -> dict:
    return {
        "id": item.id,
        "student_id": item.student_id,
        "student_name": student_name,
        "applicant_user_id": item.applicant_user_id,
        "leave_type": item.leave_type,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "reason": item.reason,
        "status": item.status,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "review_note": item.review_note,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("")
def list_leaves(
    status: str = Query("approved"),
    classroom_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    session = get_session()
    try:
        q = session.query(StudentLeaveRequest, Student).join(
            Student, Student.id == StudentLeaveRequest.student_id
        )
        if status:
            q = q.filter(StudentLeaveRequest.status == status)

        if not is_unrestricted(current_user):
            allowed = accessible_classroom_ids(session, current_user)
            if classroom_id is not None:
                if classroom_id not in allowed:
                    raise HTTPException(status_code=403, detail="您無權存取此班級")
                q = q.filter(Student.classroom_id == classroom_id)
            else:
                if not allowed:
                    return {"items": [], "total": 0}
                q = q.filter(Student.classroom_id.in_(allowed))
        elif classroom_id is not None:
            q = q.filter(Student.classroom_id == classroom_id)

        rows = q.order_by(StudentLeaveRequest.created_at.desc()).limit(limit).all()
        return {
            "items": [
                _serialize(item, student_name=student.name) for item, student in rows
            ],
            "total": len(rows),
        }
    finally:
        session.close()
