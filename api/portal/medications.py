"""api/portal/medications.py — 教師端用藥執行入口

將今日用藥 log 按班級分組，方便教師端 UI 顯示「我的班級今日用藥清單」。
實際打卡 endpoint（administer / skip / correct）走 api/student_health.py。
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
from utils.portfolio_access import accessible_classroom_ids, is_unrestricted

from ._shared import _get_employee

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
    today = date_cls.today()  # noqa: DTZ011
    session = get_session()
    try:
        # 借用 portfolio_access bridge：依 STUDENTS_HEALTH_READ scope 解析可看班級
        #   unrestricted（admin/wildcard 或自訂角色持 STUDENTS_HEALTH_READ:all）
        #     → classroom_ids=None（不限制），帶 classroom_id 則只看該班
        #   teacher（持 :own_class 或未持 scope）→ 僅看自任導師的班；帶他班 403
        unrestricted = is_unrestricted(
            current_user, code=Permission.STUDENTS_HEALTH_READ.value
        )
        my_classrooms = accessible_classroom_ids(
            session, current_user, code=Permission.STUDENTS_HEALTH_READ.value
        )
        # _get_employee 仍呼叫一次，維持原審計（unauthorized employee → 內部 raise）
        _get_employee(session, current_user)

        classroom_ids: list[int] | None
        if unrestricted:
            classroom_ids = [classroom_id] if classroom_id else None
        else:
            if classroom_id and classroom_id not in my_classrooms:
                raise HTTPException(status_code=403, detail="此班級不屬於您")
            classroom_ids = [classroom_id] if classroom_id else my_classrooms
            if not classroom_ids:
                return {"date": today.isoformat(), "groups": []}

        # Student 查詢：unrestricted + 未指定 classroom_id 時不加 classroom_id filter
        q = session.query(Student).filter(
            Student.is_active.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        if classroom_ids is not None:
            q = q.filter(Student.classroom_id.in_(classroom_ids))
        students = q.all()

        # 若 unrestricted + 未指定 classroom_id，從學生反推真實出現的班級集合
        # （避免查全表 Classroom + 回傳空 group 噪音）
        if classroom_ids is None:
            classroom_ids = sorted({s.classroom_id for s in students if s.classroom_id})

        classrooms = {
            c.id: c
            for c in session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        }
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

        request.state.audit_summary = "portal.medications.list"
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
