"""api/parent_portal/home.py — 家長首頁彙總端點。

把 me / children / 摘要（未讀公告 / 未繳費 / 待簽閱事件）合成一支，
首頁從 3-4 RTT 縮成 1 RTT，LIFF 慢網下體感差距明顯。

實作不重新查詢 — 各 count 重用對應 router 抽出的 helper：
- announcements.count_unread_for_user
- fees.compute_fees_summary
- events.count_pending_acks_for_user

Perf：home_summary 的 9 個內部 query 用 60s in-process TTLCache
壓制；接受最多 60s 的陳舊（公告/未讀/繳費等顯示 count，業主可接受）。
"""

from datetime import date, datetime, timedelta

from cachetools import TTLCache
from fastapi import APIRouter, Depends

from models.activity import ActivityRegistration, RegistrationCourse
from models.classroom import StudentAttendance
from models.database import Classroom, Guardian, Student, get_session
from models.dismissal import StudentDismissalCall
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from utils.auth import require_parent_role

from services.parent_message_service import count_unread_for_parent

from ._shared import (
    _get_parent_student_ids,
    _get_parent_user,
    resolve_parent_display_name,
)
from .announcements import count_unread_for_user as count_unread_announcements
from .events import count_pending_acks_for_user as count_pending_event_acks
from .fees import compute_fees_summary

router = APIRouter(prefix="/home", tags=["parent-home"])

# user_id → (home_summary_payload)；60s TTL，maxsize=512（同時上線家長上限）
_home_summary_cache: TTLCache = TTLCache(maxsize=512, ttl=60)


def _count_pending_activity_promotions(session, student_ids: list[int]) -> int:
    """RegistrationCourse 在 promoted_pending 狀態（候補升正式但家長未確認）的數量。

    confirm_deadline 已過期者也算進去 — 家長仍應點進去看（會看到「期限已過」訊息），
    避免家長以為系統漏通知。
    """
    if not student_ids:
        return 0
    return (
        session.query(RegistrationCourse.id)
        .join(
            ActivityRegistration,
            ActivityRegistration.id == RegistrationCourse.registration_id,
        )
        .filter(
            ActivityRegistration.is_active == True,  # noqa: E712
            ActivityRegistration.student_id.in_(student_ids),
            RegistrationCourse.status == "promoted_pending",
        )
        .count()
    )


def _count_recent_leave_reviews(session, user_id: int, days: int = 7) -> int:
    """最近 N 天內 reviewed（approved/rejected）的請假數量。

    僅統計家長自己提交的請假；用 created_at >= cutoff 而非 reviewed_at，避免
    跨期殘留卡片（家長若 7 天前提的假今天才被批准，仍應該被提醒一次）。
    家長進入 leaves 列表後 UI 自然消化，不另設 seen 旗標。
    """
    cutoff = datetime.now() - timedelta(days=days)
    return (
        session.query(StudentLeaveRequest.id)
        .filter(
            StudentLeaveRequest.applicant_user_id == user_id,
            StudentLeaveRequest.reviewed_at.isnot(None),
            StudentLeaveRequest.reviewed_at >= cutoff,
            StudentLeaveRequest.status.in_(("approved", "rejected")),
        )
        .count()
    )


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
    cached = _home_summary_cache.get(user_id)
    if cached is not None:
        return cached

    session = get_session()
    try:
        user = _get_parent_user(session, current_user)
        me = {
            "user_id": user.id,
            "name": resolve_parent_display_name(session, user),
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

        result = {
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
                "pending_activity_promotions": _count_pending_activity_promotions(
                    session, student_ids
                ),
                "recent_leave_reviews": _count_recent_leave_reviews(session, user_id),
            },
        }
        _home_summary_cache[user_id] = result
        return result
    finally:
        session.close()


@router.get("/today-status")
def today_status(current_user: dict = Depends(require_parent_role())):
    """每位子女今日的彙總狀態：出席 / 請假 / 用藥 / 接送通知。

    各狀態彼此獨立，前端用 chips 並列顯示；今日無任何狀態 → 全部回 None/空。
    回傳結構：
    ```
    {
      "date": "2026-05-01",
      "children": [
        {
          "student_id": int,
          "name": str,
          "classroom_name": str | None,
          "attendance": { "status": "出席" | ... } | null,
          "leave": { "id": int, "type": "病假" | "事假", "status": "approved" } | null,
          "medication": { "order_count": int, "has_order": bool },
          "dismissal": { "id": int, "status": "pending|acknowledged|completed", "requested_at": iso } | null,
        }, ...
      ]
    }
    ```
    """
    user_id = current_user["user_id"]
    today = date.today()
    session = get_session()
    try:
        # 子女清單（沿用 home_summary 的 join，輕量重複比共用 helper 簡單）
        rows = (
            session.query(Guardian, Student, Classroom)
            .join(Student, Student.id == Guardian.student_id)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(Guardian.user_id == user_id, Guardian.deleted_at.is_(None))
            .order_by(Student.name.asc())
            .all()
        )
        if not rows:
            return {"date": today.isoformat(), "children": []}

        student_ids = list({s.id for _, s, _ in rows})

        # 一次撈完所有子女今日 attendance / leave / medication / dismissal，避免 N+1
        att_map = {
            a.student_id: a
            for a in session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id.in_(student_ids),
                StudentAttendance.date == today,
            )
            .all()
        }
        leave_map: dict[int, StudentLeaveRequest] = {}
        for lr in (
            session.query(StudentLeaveRequest)
            .filter(
                StudentLeaveRequest.student_id.in_(student_ids),
                StudentLeaveRequest.status == "approved",
                StudentLeaveRequest.start_date <= today,
                StudentLeaveRequest.end_date >= today,
            )
            .all()
        ):
            # 同一天若多筆只保留最早一筆即可（leaves 系統已防重疊 approved）
            leave_map.setdefault(lr.student_id, lr)

        from sqlalchemy import func

        med_rows = (
            session.query(
                StudentMedicationOrder.student_id, func.count(StudentMedicationOrder.id)
            )
            .filter(
                StudentMedicationOrder.student_id.in_(student_ids),
                StudentMedicationOrder.order_date == today,
            )
            .group_by(StudentMedicationOrder.student_id)
            .all()
        )
        med_count: dict[int, int] = {sid: int(c) for sid, c in med_rows}

        # 今日進行中的接送通知（pending / acknowledged，未完成、未取消）
        dismissal_map: dict[int, StudentDismissalCall] = {}
        today_start = datetime.combine(today, datetime.min.time())
        for d in (
            session.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.student_id.in_(student_ids),
                StudentDismissalCall.requested_at >= today_start,
                StudentDismissalCall.status.in_(
                    ("pending", "acknowledged", "completed")
                ),
            )
            .order_by(StudentDismissalCall.requested_at.desc())
            .all()
        ):
            dismissal_map.setdefault(d.student_id, d)

        children = []
        for g, s, c in rows:
            sid = s.id
            att = att_map.get(sid)
            leave = leave_map.get(sid)
            d = dismissal_map.get(sid)
            children.append(
                {
                    "student_id": sid,
                    "name": s.name,
                    "classroom_name": c.name if c else None,
                    "attendance": ({"status": att.status} if att else None),
                    "leave": (
                        {
                            "id": leave.id,
                            "type": leave.leave_type,
                            "status": leave.status,
                        }
                        if leave
                        else None
                    ),
                    "medication": {
                        "has_order": sid in med_count,
                        "order_count": med_count.get(sid, 0),
                    },
                    "dismissal": (
                        {
                            "id": d.id,
                            "status": d.status,
                            "requested_at": d.requested_at.isoformat(),
                            "acknowledged_at": (
                                d.acknowledged_at.isoformat()
                                if d.acknowledged_at
                                else None
                            ),
                            "completed_at": (
                                d.completed_at.isoformat() if d.completed_at else None
                            ),
                        }
                        if d
                        else None
                    ),
                }
            )

        return {"date": today.isoformat(), "children": children}
    finally:
        session.close()
