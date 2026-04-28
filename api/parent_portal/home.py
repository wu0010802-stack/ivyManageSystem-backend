"""api/parent_portal/home.py — 家長首頁彙總端點。

把 me / children / 摘要（未讀公告 / 未繳費 / 待簽閱事件）合成一支，
首頁從 3-4 RTT 縮成 1 RTT，LIFF 慢網下體感差距明顯。

實作不重新查詢 — 各 count 重用對應 router 抽出的 helper：
- announcements.count_unread_for_user
- fees.compute_fees_summary
- events.count_pending_acks_for_user
"""

from fastapi import APIRouter, Depends

from models.database import Classroom, Guardian, Student, get_session
from utils.auth import require_parent_role

from services.parent_message_service import count_unread_for_parent

from ._shared import _get_parent_student_ids, _get_parent_user
from .announcements import count_unread_for_user as count_unread_announcements
from .events import count_pending_acks_for_user as count_pending_event_acks
from .fees import compute_fees_summary

router = APIRouter(prefix="/home", tags=["parent-home"])


@router.get("/summary")
def home_summary(current_user: dict = Depends(require_parent_role())):
    """家長首頁一站式彙總。

    回傳：
    - me: 個人資料 + 推播可達性（同 /me）
    - children: 監護學生清單（同 /my-children）
    - summary:
        - unread_announcements: int
        - fees: { outstanding, overdue, due_soon, outstanding_count, ... }
        - pending_event_acks: int
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        user = _get_parent_user(session, current_user)
        me = {
            "user_id": user.id,
            "name": user.username,
            "line_user_id": user.line_user_id,
            "role": "parent",
            "can_push": user.line_follow_confirmed_at is not None,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        }

        rows = (
            session.query(Guardian, Student, Classroom)
            .join(Student, Student.id == Guardian.student_id)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(Guardian.user_id == user_id, Guardian.deleted_at.is_(None))
            .order_by(Student.enrollment_date.asc().nulls_last(), Student.name.asc())
            .all()
        )
        children = [
            {
                "guardian_id": g.id,
                "guardian_relation": g.relation,
                "is_primary": bool(g.is_primary),
                "can_pickup": bool(g.can_pickup),
                "student_id": s.id,
                "student_no": s.student_id,
                "name": s.name,
                "gender": s.gender,
                "birthday": s.birthday.isoformat() if s.birthday else None,
                "classroom_id": c.id if c else None,
                "classroom_name": c.name if c else None,
                "lifecycle_status": s.lifecycle_status,
                "is_active": bool(s.is_active),
            }
            for g, s, c in rows
        ]

        _, student_ids = _get_parent_student_ids(session, user_id)
        fees = compute_fees_summary(session, student_ids)

        return {
            "me": me,
            "children": children,
            "summary": {
                "unread_announcements": count_unread_announcements(session, user_id),
                "fees": fees["totals"],
                "pending_event_acks": count_pending_event_acks(
                    session, user_id, student_ids
                ),
                "unread_messages": count_unread_for_parent(
                    session, parent_user_id=user_id
                ),
            },
        }
    finally:
        session.close()
