"""api/portal/medications.py — 教師端用藥執行入口

將今日用藥 log 按班級分組，方便教師端 UI 顯示「我的班級今日用藥清單」。
實際打卡 endpoint（administer / skip / correct）走 api/student_health.py。
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from fastapi import APIRouter, Depends, Query, Request

from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Classroom,
    Student,
    User,
    get_session,
)
from models.portfolio import StudentMedicationLog, StudentMedicationOrder
from utils.auth import require_permission
from utils.permissions import Permission

from ._shared import _get_employee, _get_teacher_classroom_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/medications", tags=["portal-medications"])


def _log_status(log: StudentMedicationLog) -> str:
    if log.skipped:
        return "skipped"
    if log.administered_at is not None:
        return "administered"
    return "pending"


@router.get("/today")
def list_today_medications(
    request: Request,
    classroom_id: int | None = Query(None, gt=0),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_READ)),
):
    """教師當日用藥任務，按班級分組。

    classroom_id 帶入時只回該班；否則回教師管轄全部班級。
    教師僅可看自己班級；admin/supervisor 可帶任意 classroom_id。
    """
    today = date_cls.today()
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        is_admin_like = int(
            current_user.get("permissions", 0) or 0
        ) < 0 or current_user.get("role") in ("admin", "supervisor")

        if is_admin_like and classroom_id:
            classroom_ids = [classroom_id]
        else:
            my_classrooms = _get_teacher_classroom_ids(session, emp.id)
            if classroom_id:
                if classroom_id not in my_classrooms:
                    from fastapi import HTTPException

                    raise HTTPException(status_code=403, detail="此班級不屬於您")
                classroom_ids = [classroom_id]
            else:
                classroom_ids = my_classrooms

        if not classroom_ids:
            return {"date": today.isoformat(), "groups": []}

        classrooms = {
            c.id: c
            for c in session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        }
        students = (
            session.query(Student)
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active.is_(True),
                Student.lifecycle_status == LIFECYCLE_ACTIVE,
            )
            .all()
        )
        students_by_id = {s.id: s for s in students}
        if not students:
            return {
                "date": today.isoformat(),
                "groups": [
                    {
                        "classroom_id": cid,
                        "classroom_name": (
                            classrooms[cid].name if cid in classrooms else ""
                        ),
                        "items": [],
                        "stats": {"pending": 0, "administered": 0, "skipped": 0},
                    }
                    for cid in classroom_ids
                ],
            }

        student_ids = list(students_by_id.keys())
        orders = (
            session.query(StudentMedicationOrder)
            .filter(
                StudentMedicationOrder.student_id.in_(student_ids),
                StudentMedicationOrder.order_date == today,
            )
            .all()
        )
        orders_by_id = {o.id: o for o in orders}
        if not orders:
            return {
                "date": today.isoformat(),
                "groups": [
                    {
                        "classroom_id": cid,
                        "classroom_name": (
                            classrooms[cid].name if cid in classrooms else ""
                        ),
                        "items": [],
                        "stats": {"pending": 0, "administered": 0, "skipped": 0},
                    }
                    for cid in classroom_ids
                ],
            }

        logs = (
            session.query(StudentMedicationLog)
            .filter(
                StudentMedicationLog.order_id.in_(orders_by_id.keys()),
                StudentMedicationLog.correction_of.is_(None),
            )
            .order_by(StudentMedicationLog.scheduled_time.asc())
            .all()
        )

        # 預載執行人姓名（如有）
        admin_user_ids = {
            l.administered_by for l in logs if l.administered_by is not None
        }
        admin_user_names: dict[int, str] = {}
        if admin_user_ids:
            for u in session.query(User).filter(User.id.in_(admin_user_ids)).all():
                admin_user_names[u.id] = u.username

        # 分班彙整
        items_by_classroom: dict[int, list[dict]] = {cid: [] for cid in classroom_ids}
        stats_by_classroom: dict[int, dict[str, int]] = {
            cid: {"pending": 0, "administered": 0, "skipped": 0}
            for cid in classroom_ids
        }
        for log in logs:
            order = orders_by_id.get(log.order_id)
            if not order:
                continue
            student = students_by_id.get(order.student_id)
            if not student:
                continue
            cid = student.classroom_id
            if cid not in items_by_classroom:
                continue
            status = _log_status(log)
            stats_by_classroom[cid][status] += 1
            items_by_classroom[cid].append(
                {
                    "log_id": log.id,
                    "order_id": order.id,
                    "student_id": student.id,
                    "student_name": student.name,
                    "scheduled_time": log.scheduled_time,
                    "medication_name": order.medication_name,
                    "dose": order.dose,
                    "note": order.note,
                    "source": order.source,
                    "status": status,
                    "administered_at": (
                        log.administered_at.isoformat() if log.administered_at else None
                    ),
                    "administered_by_name": admin_user_names.get(log.administered_by),
                    "skipped_reason": log.skipped_reason,
                }
            )

        request.state.audit_skip = True
        return {
            "date": today.isoformat(),
            "groups": [
                {
                    "classroom_id": cid,
                    "classroom_name": classrooms[cid].name if cid in classrooms else "",
                    "items": items_by_classroom[cid],
                    "stats": stats_by_classroom[cid],
                }
                for cid in classroom_ids
            ],
        }
    finally:
        session.close()
