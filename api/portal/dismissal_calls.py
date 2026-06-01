"""
api/portal/dismissal_calls.py — 教師 portal 接送通知 HTTP endpoints
"""

import asyncio
import logging
from datetime import datetime, date
from utils.taipei_time import today_taipei

from fastapi import APIRouter, Depends, HTTPException

from models.database import get_session, Classroom, Student
from models.dismissal import StudentDismissalCall, _now_taipei_naive, _TAIPEI_TZ
from utils.auth import require_permission
from utils.permissions import Permission
from api.dismissal_calls import _call_base_dict, _DAY_START, _DAY_END
from ._shared import _get_teacher_classroom_ids, _get_employee
from utils.portfolio_access import is_unrestricted

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 輔助函式
# ---------------------------------------------------------------------------


def _build_calls_out_bulk(calls: list, session) -> list[dict]:
    """批量組裝 API 回傳 dict，避免 N+1 查詢（用於列表端點）。"""
    if not calls:
        return []

    student_ids = {c.student_id for c in calls}
    classroom_ids = {c.classroom_id for c in calls}

    students = {
        s.id: s
        for s in session.query(Student).filter(Student.id.in_(student_ids)).all()
    }
    classrooms = {
        c.id: c
        for c in session.query(Classroom).filter(Classroom.id.in_(classroom_ids)).all()
    }

    return [
        _call_base_dict(
            call, students.get(call.student_id), classrooms.get(call.classroom_id)
        )
        for call in calls
    ]


def _get_manager():
    from api.dismissal_ws import manager

    return manager


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/dismissal-calls")
def portal_list_dismissal_calls(
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_READ)),
):
    """列出我的班級今日的 pending + acknowledged 通知。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # Phase 2.3：DISMISSAL_CALLS_READ:all 可跨班看全校；teacher :own_class 限自班
        if is_unrestricted(current_user, code=Permission.DISMISSAL_CALLS_READ.value):
            classroom_ids = None
        else:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if not classroom_ids:
                return []

        today = today_taipei()
        day_start = datetime.combine(today, _DAY_START)
        day_end = datetime.combine(today, _DAY_END)

        q = (
            session.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.status.in_(["pending", "acknowledged"]),
                StudentDismissalCall.requested_at >= day_start,
                StudentDismissalCall.requested_at <= day_end,
            )
        )
        if classroom_ids is not None:
            q = q.filter(StudentDismissalCall.classroom_id.in_(classroom_ids))

        calls = q.order_by(StudentDismissalCall.requested_at.desc()).all()

        return _build_calls_out_bulk(calls, session)
    finally:
        session.close()


@router.get("/dismissal-calls/pending-count")
def portal_pending_count(
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_READ)),
):
    """我的班級今日 pending 狀態通知數量。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # Phase 2.3：DISMISSAL_CALLS_READ:all 可跨班看全校；teacher :own_class 限自班
        if is_unrestricted(current_user, code=Permission.DISMISSAL_CALLS_READ.value):
            classroom_ids = None
        else:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if not classroom_ids:
                return {"count": 0}

        today = today_taipei()
        day_start = datetime.combine(today, _DAY_START)
        day_end = datetime.combine(today, _DAY_END)

        q = session.query(StudentDismissalCall).filter(
            StudentDismissalCall.status == "pending",
            StudentDismissalCall.requested_at >= day_start,
            StudentDismissalCall.requested_at <= day_end,
        )
        if classroom_ids is not None:
            q = q.filter(StudentDismissalCall.classroom_id.in_(classroom_ids))

        count = q.count()
        return {"count": count}
    finally:
        session.close()


def _db_transition_call(
    call_id: int,
    current_user: dict,
    *,
    required_status: str,
    new_status: str,
    by_field: str,
    at_field: str,
    action_label: str,
) -> tuple[dict, int]:
    """驗證教師權限 → 查詢通知 → 確認狀態 → 更新狀態，回傳 (out_dict, classroom_id)。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # Phase 2.3：DISMISSAL_CALLS_WRITE:all 可跨班處理全校；teacher :own_class 限自班
        if is_unrestricted(current_user, code=Permission.DISMISSAL_CALLS_WRITE.value):
            classroom_ids = None
        else:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        call = (
            session.query(StudentDismissalCall)
            .filter(StudentDismissalCall.id == call_id)
            .with_for_update()
            .first()
        )
        # F-006：「通知不存在」與「屬於別班」collapse 為單一 403 generic，
        # 避免透過 status code 差異枚舉 StudentDismissalCall id 存在性。
        # 422（own-class 但狀態不符）保留為合法業務流程錯誤，不算 enum oracle。
        # unrestricted=True 時跳過 classroom 比對（仍保留 not call → 403 防 enum oracle）
        if not call or (
            classroom_ids is not None and call.classroom_id not in classroom_ids
        ):
            raise HTTPException(status_code=403, detail="查無此通知或無權存取")
        # 列鎖（bug sweep 2026-05-12 round 3）：兩位老師同時點 acknowledge / complete
        # 同一筆通知時，無鎖會讓 acknowledged_by / completed_by 被後贏者覆蓋稽核軌跡。
        if call.status != required_status:
            raise HTTPException(
                status_code=422,
                detail=f"狀態為 {call.status} 的通知無法執行{action_label}操作",
            )

        # 在 commit 前預讀，避免 post-commit N+1 查詢
        student = session.query(Student).filter(Student.id == call.student_id).first()
        classroom = (
            session.query(Classroom).filter(Classroom.id == call.classroom_id).first()
        )

        classroom_id = call.classroom_id
        call.status = new_status
        setattr(call, by_field, emp.id)
        setattr(call, at_field, _now_taipei_naive())
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        out = _call_base_dict(call, student, classroom)
        logger.info("接送通知%s：ID %d，教師 %s", action_label, call_id, emp.name)
        return out, classroom_id
    finally:
        session.close()


def _db_acknowledge(call_id: int, current_user: dict) -> tuple[dict, int]:
    return _db_transition_call(
        call_id,
        current_user,
        required_status="pending",
        new_status="acknowledged",
        by_field="acknowledged_by_employee_id",
        at_field="acknowledged_at",
        action_label="已收到",
    )


def _db_complete(call_id: int, current_user: dict) -> tuple[dict, int]:
    return _db_transition_call(
        call_id,
        current_user,
        required_status="acknowledged",
        new_status="completed",
        by_field="completed_by_employee_id",
        at_field="completed_at",
        action_label="已放學",
    )


@router.post("/dismissal-calls/{call_id}/acknowledge")
async def portal_acknowledge(
    call_id: int,
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_WRITE)),
):
    """老師確認已收到接送通知（pending → acknowledged）。"""
    loop = asyncio.get_running_loop()
    out, classroom_id = await loop.run_in_executor(
        None, _db_acknowledge, call_id, current_user
    )

    await _get_manager().broadcast(
        classroom_id,
        {
            "type": "dismissal_call_updated",
            "payload": out,
        },
    )
    return out


@router.post("/dismissal-calls/{call_id}/complete")
async def portal_complete(
    call_id: int,
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_WRITE)),
):
    """老師確認學生已放學（acknowledged → completed）。"""
    loop = asyncio.get_running_loop()
    out, classroom_id = await loop.run_in_executor(
        None, _db_complete, call_id, current_user
    )

    await _get_manager().broadcast(
        classroom_id,
        {
            "type": "dismissal_call_updated",
            "payload": out,
        },
    )
    return out
