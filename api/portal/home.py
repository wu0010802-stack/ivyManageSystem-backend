"""api/portal/home.py — 教師首頁（今日待辦 dashboard）彙總端點

GET /api/portal/home/summary
回傳教師當日 dashboard 所需資訊：
- me：使用者基本資料
- today：今日班次 + 我的考勤狀態
- classrooms：每個管轄班級的營運卡（聯絡簿率/未點名/接送/連續缺席/生日/過敏/用藥）
- actions：跨站待辦計數（未讀訊息/代理/換班/未讀公告/異常確認）
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from fastapi import APIRouter, Depends, Request

from models.database import (
    Announcement,
    AnnouncementRead,
    AnnouncementRecipient,
    Attendance,
    Classroom,
    Employee,
    LeaveRecord,
    ShiftSwapRequest,
    Student,
    StudentDismissalCall,
    User,
    get_session,
)
from services.contact_book_service import compute_class_completion
from services.parent_message_service import count_unread_for_teacher
from services.portal_dashboard_service import (
    compute_allergy_alerts,
    compute_consecutive_absences,
    compute_upcoming_birthdays,
    count_pending_medications,
    has_attendance_today,
)
from utils.auth import get_current_user
from utils.permissions import Permission

from ._shared import (
    _get_employee,
    _get_employee_shift_for_date,
    _get_shift_type_map,
    _get_teacher_classroom_ids,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portal-home"])


def _today_shift_for_employee(
    session, employee_id: int, today: date_cls
) -> dict | None:
    shift_id = _get_employee_shift_for_date(session, employee_id, today)
    if not shift_id:
        return None
    shift_map = _get_shift_type_map(session, active_only=False)
    st = shift_map.get(shift_id)
    if not st:
        return None
    return {
        "shift_type_id": st.id,
        "name": st.name,
        "work_start": str(st.work_start) if st.work_start else None,
        "work_end": str(st.work_end) if st.work_end else None,
    }


def _my_attendance_today(session, employee_id: int, today: date_cls) -> dict:
    rec = (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == today,
        )
        .first()
    )
    if not rec:
        return {"punch_in_at": None, "punch_out_at": None, "is_anomaly": False}
    is_anomaly = bool(
        rec.is_late
        or rec.is_early_leave
        or rec.is_missing_punch_in
        or rec.is_missing_punch_out
    )
    return {
        "punch_in_at": rec.punch_in_time.isoformat() if rec.punch_in_time else None,
        "punch_out_at": rec.punch_out_time.isoformat() if rec.punch_out_time else None,
        "is_anomaly": is_anomaly,
    }


def _classroom_card(classroom: Classroom, today: date_cls, batches: dict) -> dict:
    """純資料組裝；所有 query 由呼叫端預先 batch。

    `batches` 必須含以下 keys（每個 value 為 dict[classroom_id, T]）:
      - student_count: dict[int, int]
      - completion: dict[int, dict]
      - pending_dismissal: dict[int, int]
      - attendance_called: dict[int, bool]
      - consecutive_absences: dict[int, list[dict]]
      - upcoming_birthdays: dict[int, list[dict]]
      - allergy_alerts: dict[int, list[dict]]
      - pending_medications: dict[int, int]
    """
    cid = classroom.id
    completion = batches["completion"].get(
        cid, {"roster": 0, "draft": 0, "published": 0, "missing": 0}
    )
    roster = completion.get("roster", 0)
    return {
        "classroom_id": cid,
        "classroom_name": classroom.name,
        "student_count": batches["student_count"].get(cid, 0),
        "contact_book": {
            "roster": roster,
            "draft": completion.get("draft", 0),
            "published": completion.get("published", 0),
            "missing": completion.get("missing", 0),
            "percentage": (
                round(completion.get("published", 0) / roster * 100, 1)
                if roster
                else 0.0
            ),
        },
        "attendance_called_today": batches["attendance_called"].get(cid, False),
        "pending_dismissal_calls": batches["pending_dismissal"].get(cid, 0),
        "consecutive_absences": batches["consecutive_absences"].get(cid, []),
        "upcoming_birthdays_7d": batches["upcoming_birthdays"].get(cid, []),
        "allergy_alerts": batches["allergy_alerts"].get(cid, []),
        "pending_medications_today": batches["pending_medications"].get(cid, 0),
    }


def _count_unread_announcements(session, employee_id: int) -> int:
    """未讀公告計數（沿用 portal/announcements unread-count 結構）。

    visible = 沒有 recipients 的公告 OR 此員工是被指定的對象
    unread = visible 公告數 - 此員工已讀過的數量
    """
    no_recipients_subq = (
        ~session.query(AnnouncementRecipient)
        .filter(AnnouncementRecipient.announcement_id == Announcement.id)
        .exists()
    )
    targeted_to_me_subq = (
        session.query(AnnouncementRecipient)
        .filter(
            AnnouncementRecipient.announcement_id == Announcement.id,
            AnnouncementRecipient.employee_id == employee_id,
        )
        .exists()
    )
    visible_filter = no_recipients_subq | targeted_to_me_subq
    total = session.query(Announcement).filter(visible_filter).count()
    read = (
        session.query(AnnouncementRead)
        .filter(AnnouncementRead.employee_id == employee_id)
        .count()
    )
    return max(0, total - read)


def _count_pending_anomalies(session, employee_id: int) -> int:
    """待確認的考勤異常筆數（任一 anomaly flag 為 true 且 confirmed_at IS NULL）。"""
    from sqlalchemy import or_

    return (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == employee_id,
            Attendance.confirmed_at.is_(None),
            or_(
                Attendance.is_late.is_(True),
                Attendance.is_early_leave.is_(True),
                Attendance.is_missing_punch_in.is_(True),
                Attendance.is_missing_punch_out.is_(True),
            ),
        )
        .count()
    )


@router.get("/home/summary")
def get_home_summary(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """教師首頁彙總。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        today = date_cls.today()

        user = session.query(User).filter(User.id == user_id).first()

        # 我的班級
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids), Classroom.is_active.is_(True))
            .order_by(Classroom.name.asc())
            .all()
            if classroom_ids
            else []
        )
        # batch 預取所有班級 dashboard 資料（避免 _classroom_card N+1）
        ids = [c.id for c in classrooms]
        if ids:
            from sqlalchemy import func

            student_counts = dict(
                session.query(Student.classroom_id, func.count(Student.id))
                .filter(
                    Student.classroom_id.in_(ids),
                    Student.is_active.is_(True),
                )
                .group_by(Student.classroom_id)
                .all()
            )
            pending_dismissal = dict(
                session.query(
                    StudentDismissalCall.classroom_id,
                    func.count(StudentDismissalCall.id),
                )
                .filter(
                    StudentDismissalCall.classroom_id.in_(ids),
                    StudentDismissalCall.status == "pending",
                )
                .group_by(StudentDismissalCall.classroom_id)
                .all()
            )
            batches = {
                "student_count": student_counts,
                "completion": compute_class_completion(
                    session, classroom_id=ids, log_date=today
                ),
                "pending_dismissal": pending_dismissal,
                "attendance_called": has_attendance_today(
                    session, classroom_id=ids, today=today
                ),
                "consecutive_absences": compute_consecutive_absences(
                    session, classroom_id=ids, today=today
                ),
                "upcoming_birthdays": compute_upcoming_birthdays(
                    session, classroom_id=ids, today=today
                ),
                "allergy_alerts": compute_allergy_alerts(session, classroom_id=ids),
                "pending_medications": count_pending_medications(
                    session, classroom_id=ids, today=today
                ),
            }
        else:
            batches = {
                "student_count": {},
                "completion": {},
                "pending_dismissal": {},
                "attendance_called": {},
                "consecutive_absences": {},
                "upcoming_birthdays": {},
                "allergy_alerts": {},
                "pending_medications": {},
            }

        classroom_cards = [_classroom_card(c, today, batches) for c in classrooms]

        # actions：跨站待辦計數
        unread_messages = 0
        if (
            int(current_user.get("permissions", 0) or 0)
            & int(Permission.PARENT_MESSAGES_WRITE.value)
            or int(current_user.get("permissions", 0) or 0) < 0
        ):
            unread_messages = count_unread_for_teacher(session, teacher_user_id=user_id)

        pending_substitute = (
            session.query(LeaveRecord)
            .filter(
                LeaveRecord.substitute_employee_id == emp.id,
                LeaveRecord.substitute_status == "pending",
            )
            .count()
        )
        pending_swap = (
            session.query(ShiftSwapRequest)
            .filter(
                ShiftSwapRequest.target_id == emp.id,
                ShiftSwapRequest.status == "pending",
            )
            .count()
        )
        unread_announcements = _count_unread_announcements(session, emp.id)
        pending_anomaly_confirms = _count_pending_anomalies(session, emp.id)

        # 我自己今日班次與打卡
        today_shift = _today_shift_for_employee(session, emp.id, today)
        my_attendance = _my_attendance_today(session, emp.id, today)

        request.state.audit_skip = True

        return {
            "me": {
                "user_id": user_id,
                "employee_id": emp.id,
                "name": emp.name,
                "username": user.username if user else None,
                "role": current_user.get("role"),
                "must_change_password": bool(current_user.get("must_change_password")),
            },
            "today": {
                "date": today.isoformat(),
                "shift": today_shift,
                "attendance": my_attendance,
            },
            "classrooms": classroom_cards,
            "actions": {
                "unread_messages": unread_messages,
                "pending_substitute": pending_substitute,
                "pending_swap": pending_swap,
                "unread_announcements": unread_announcements,
                "pending_anomaly_confirms": pending_anomaly_confirms,
            },
        }
    finally:
        session.close()
