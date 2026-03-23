"""
api/dismissal_calls.py — 管理端接送通知 HTTP endpoints
"""

import asyncio
import logging
from datetime import datetime, date, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import (
    get_session, Student, Classroom, User, Employee,
)
from models.dismissal import StudentDismissalCall
from utils.auth import require_permission, get_current_user
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dismissal-calls", tags=["dismissal-calls"])

# 日期邊界常數（每日查詢範圍）
_DAY_START = time(0, 0, 0)
_DAY_END   = time(23, 59, 59)

_line_service = None


def init_dismissal_line_service(line_service) -> None:
    global _line_service
    _line_service = line_service


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class DismissalCallCreate(BaseModel):
    student_id: int
    classroom_id: int
    note: Optional[str] = None


class DismissalCallOut(BaseModel):
    id: int
    student_id: int
    student_name: str
    classroom_id: int
    classroom_name: str
    status: str
    requested_at: datetime
    requested_by_name: str
    acknowledged_at: Optional[datetime]
    completed_at: Optional[datetime]
    note: Optional[str]

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# 輔助函式
# ---------------------------------------------------------------------------

def _call_base_dict(call: StudentDismissalCall, student, classroom) -> dict:
    """接送通知公共欄位（管理端與教師 portal 共用）。"""
    return {
        "id": call.id,
        "student_id": call.student_id,
        "student_name": student.name if student else "未知學生",
        "classroom_id": call.classroom_id,
        "classroom_name": classroom.name if classroom else "未知班級",
        "status": call.status,
        "requested_at": call.requested_at,
        "acknowledged_at": call.acknowledged_at,
        "completed_at": call.completed_at,
        "note": call.note,
    }


def _build_call_out(call: StudentDismissalCall, session) -> dict:
    """將單筆 ORM 物件組成 API 回傳 dict（用於單筆操作）。"""
    student = session.query(Student).filter(Student.id == call.student_id).first()
    classroom = session.query(Classroom).filter(Classroom.id == call.classroom_id).first()
    requester = session.query(User).filter(User.id == call.requested_by_user_id).first()

    # 取得請求者的員工姓名（若有），否則用 username
    if requester:
        emp = session.query(Employee).filter(Employee.id == requester.employee_id).first()
        requester_name = emp.name if emp else requester.username
    else:
        requester_name = "未知"

    return {**_call_base_dict(call, student, classroom), "requested_by_name": requester_name}


def _build_calls_out_bulk(calls: list, session) -> list[dict]:
    """批量組裝 API 回傳 dict，避免 N+1 查詢（用於列表端點）。"""
    if not calls:
        return []

    student_ids = {c.student_id for c in calls}
    classroom_ids = {c.classroom_id for c in calls}
    user_ids = {c.requested_by_user_id for c in calls if c.requested_by_user_id}

    students = {s.id: s for s in session.query(Student).filter(Student.id.in_(student_ids)).all()}
    classrooms = {c.id: c for c in session.query(Classroom).filter(Classroom.id.in_(classroom_ids)).all()}
    users = {u.id: u for u in session.query(User).filter(User.id.in_(user_ids)).all()}

    employee_ids = {u.employee_id for u in users.values() if u.employee_id}
    employees = {e.id: e for e in session.query(Employee).filter(Employee.id.in_(employee_ids)).all()}

    result = []
    for call in calls:
        student = students.get(call.student_id)
        classroom = classrooms.get(call.classroom_id)
        user = users.get(call.requested_by_user_id)
        emp = employees.get(user.employee_id) if user and user.employee_id else None
        requester_name = emp.name if emp else (user.username if user else "未知")
        result.append({**_call_base_dict(call, student, classroom), "requested_by_name": requester_name})
    return result


def _get_manager():
    """延遲 import manager，避免循環 import。"""
    from api.dismissal_ws import manager
    return manager


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _db_create_dismissal_call(body: DismissalCallCreate, user_id: int) -> tuple[dict, int]:
    """同步 DB 操作：建立接送通知，回傳 (out_dict, classroom_id)。"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == body.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到學生")
        if student.classroom_id != body.classroom_id:
            raise HTTPException(status_code=400, detail="學生不屬於指定班級")

        existing = session.query(StudentDismissalCall).filter(
            StudentDismissalCall.student_id == body.student_id,
            StudentDismissalCall.status.in_(["pending", "acknowledged"]),
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"學生 {student.name} 已有進行中的接送通知（ID: {existing.id}）",
            )

        call = StudentDismissalCall(
            student_id=body.student_id,
            classroom_id=body.classroom_id,
            requested_by_user_id=user_id,
            note=body.note,
            status="pending",
            requested_at=datetime.now(timezone.utc),
        )
        session.add(call)
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        session.refresh(call)

        out = _build_call_out(call, session)
        logger.info(
            "接送通知建立：學生 %s (ID: %d)，班級 ID: %d，通知 ID: %d",
            out["student_name"], body.student_id, body.classroom_id, call.id,
        )
        return out, body.classroom_id
    finally:
        session.close()


@router.post("", status_code=201)
async def create_dismissal_call(
    body: DismissalCallCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """建立接送通知。同一學生若已有 pending/acknowledged 通知則拋 409。"""
    user_id = current_user.get("user_id")
    loop = asyncio.get_running_loop()
    out, classroom_id = await loop.run_in_executor(None, _db_create_dismissal_call, body, user_id)

    # WebSocket 廣播
    await _get_manager().broadcast(classroom_id, {
        "type": "dismissal_call_created",
        "payload": {**out, "requested_at": out["requested_at"].isoformat()},
    })

    # LINE 群組推播
    if _line_service is not None:
        try:
            _line_service.notify_dismissal_created(
                out["student_name"], out["classroom_name"], body.note
            )
        except Exception as _le:
            logger.warning("接送通知 LINE 推播失敗: %s", _le)

    return out


@router.get("")
def list_dismissal_calls(
    target_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設今日"),
    status: Optional[str] = Query(None, description="pending/acknowledged/completed/cancelled"),
    classroom_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """列出接送通知（預設今日）。"""
    session = get_session()
    try:
        if target_date:
            try:
                target = datetime.strptime(target_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="日期格式錯誤，應為 YYYY-MM-DD")
        else:
            target = date.today()

        day_start = datetime.combine(target, _DAY_START)
        day_end = datetime.combine(target, _DAY_END)

        q = session.query(StudentDismissalCall).filter(
            StudentDismissalCall.requested_at >= day_start,
            StudentDismissalCall.requested_at <= day_end,
        )
        if status:
            q = q.filter(StudentDismissalCall.status == status)
        if classroom_id:
            q = q.filter(StudentDismissalCall.classroom_id == classroom_id)

        calls = q.order_by(StudentDismissalCall.requested_at.desc()).all()
        return _build_calls_out_bulk(calls, session)
    finally:
        session.close()


def _db_cancel_dismissal_call(call_id: int) -> tuple[dict, int]:
    """同步 DB 操作：取消接送通知，回傳 (out_dict, classroom_id)。"""
    session = get_session()
    try:
        call = session.query(StudentDismissalCall).filter(
            StudentDismissalCall.id == call_id
        ).first()
        if not call:
            raise HTTPException(status_code=404, detail="找不到通知")
        if call.status not in ("pending", "acknowledged"):
            raise HTTPException(
                status_code=422,
                detail=f"狀態為 {call.status} 的通知無法取消",
            )

        classroom_id = call.classroom_id
        call.status = "cancelled"
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        out = _build_call_out(call, session)
        logger.info("接送通知已取消：ID %d", call_id)
        return out, classroom_id
    finally:
        session.close()


@router.post("/{call_id}/cancel")
async def cancel_dismissal_call(
    call_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """取消接送通知（僅 pending/acknowledged 狀態可取消）。"""
    loop = asyncio.get_running_loop()
    out, classroom_id = await loop.run_in_executor(None, _db_cancel_dismissal_call, call_id)

    await _get_manager().broadcast(classroom_id, {
        "type": "dismissal_call_cancelled",
        "payload": {**out, "requested_at": out["requested_at"].isoformat()},
    })
    return out
