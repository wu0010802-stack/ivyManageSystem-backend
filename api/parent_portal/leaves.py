"""api/parent_portal/leaves.py — 家長端學生請假申請。

- POST /api/parent/student-leaves（建立 pending 申請）
- GET  /api/parent/student-leaves（列出家長所有小孩的申請）
- GET  /api/parent/student-leaves/{id}
- POST /api/parent/student-leaves/{id}/cancel（僅 pending 可 cancel）

期間規則：
- start_date 不可早於今天前 30 天，不可晚於今天後 60 天
- end_date 必 >= start_date
- 同一 student 在 start_date..end_date 區間內若有 pending/approved 重疊
  → 400（避免家長重複送、避免 approve 後雙寫 attendance）
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_

from models.database import (
    Guardian,
    StudentLeaveRequest,
    StudentAttendance,
    get_session,
)
from models.student_leave import LEAVE_TYPES
from services.student_leave_service import (
    is_remark_owned_by_leave,
)
from utils.auth import require_parent_role

from ._shared import _assert_student_owned, _get_parent_student_ids

router = APIRouter(prefix="/student-leaves", tags=["parent-leaves"])


_PAST_LIMIT_DAYS = 30
_FUTURE_LIMIT_DAYS = 60


class CreateLeaveRequest(BaseModel):
    student_id: int = Field(..., gt=0)
    leave_type: str = Field(...)
    start_date: date
    end_date: date
    reason: Optional[str] = Field(None, max_length=500)

    @field_validator("leave_type")
    @classmethod
    def _check_type(cls, v):
        if v not in LEAVE_TYPES:
            raise ValueError(f"leave_type 須為 {LEAVE_TYPES} 之一")
        return v


def _validate_date_range(req: CreateLeaveRequest) -> None:
    today = date.today()
    if req.end_date < req.start_date:
        raise HTTPException(status_code=400, detail="end_date 不可早於 start_date")
    if req.start_date < today - timedelta(days=_PAST_LIMIT_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"start_date 不可早於今天前 {_PAST_LIMIT_DAYS} 天",
        )
    if req.start_date > today + timedelta(days=_FUTURE_LIMIT_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"start_date 不可晚於今天後 {_FUTURE_LIMIT_DAYS} 天",
        )


def _check_overlap(session, student_id: int, start: date, end: date) -> None:
    overlap = (
        session.query(StudentLeaveRequest)
        .filter(
            StudentLeaveRequest.student_id == student_id,
            StudentLeaveRequest.status.in_(("pending", "approved")),
            StudentLeaveRequest.start_date <= end,
            StudentLeaveRequest.end_date >= start,
        )
        .first()
    )
    if overlap is not None:
        raise HTTPException(
            status_code=400,
            detail="此期間已有其他申請（pending/approved），請先處理或調整日期",
        )


def _serialize(item: StudentLeaveRequest) -> dict:
    return {
        "id": item.id,
        "student_id": item.student_id,
        "leave_type": item.leave_type,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "reason": item.reason,
        "status": item.status,
        "review_note": item.review_note,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.post("", status_code=201)
def create_leave(
    payload: CreateLeaveRequest,
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    _validate_date_range(payload)
    session = get_session()
    try:
        _assert_student_owned(session, user_id, payload.student_id)
        _check_overlap(
            session, payload.student_id, payload.start_date, payload.end_date
        )

        guardian = (
            session.query(Guardian)
            .filter(
                Guardian.user_id == user_id,
                Guardian.student_id == payload.student_id,
                Guardian.deleted_at.is_(None),
            )
            .first()
        )
        item = StudentLeaveRequest(
            student_id=payload.student_id,
            applicant_user_id=user_id,
            applicant_guardian_id=guardian.id if guardian else None,
            leave_type=payload.leave_type,
            start_date=payload.start_date,
            end_date=payload.end_date,
            reason=(payload.reason or "").strip() or None,
            status="pending",
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return _serialize(item)
    finally:
        session.close()


@router.get("")
def list_leaves(current_user: dict = Depends(require_parent_role())):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _, student_ids = _get_parent_student_ids(session, user_id)
        if not student_ids:
            return {"items": [], "total": 0}
        rows = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.student_id.in_(student_ids))
            .order_by(StudentLeaveRequest.created_at.desc())
            .all()
        )
        return {"items": [_serialize(r) for r in rows], "total": len(rows)}
    finally:
        session.close()


@router.get("/{leave_id}")
def get_leave(
    leave_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # F-004：「申請不存在」與「不屬於本家庭」collapse 為單一 403，
        # 避免透過 status code 差異枚舉 StudentLeaveRequest id 存在性。
        _, owned_student_ids = _get_parent_student_ids(session, user_id)
        item = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.id == leave_id)
            .first()
        )
        if item is None or item.student_id not in owned_student_ids:
            raise HTTPException(status_code=403, detail="查無此資料或無權存取")
        return _serialize(item)
    finally:
        session.close()


@router.post("/{leave_id}/cancel")
def cancel_leave(
    leave_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """僅 status=='pending' 可取消（approved 後須由教師反向處理）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # F-004：同 GET，「申請不存在」與「不屬於本家庭」collapse 為單一 403。
        _, owned_student_ids = _get_parent_student_ids(session, user_id)
        item = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.id == leave_id)
            .first()
        )
        if item is None or item.student_id not in owned_student_ids:
            raise HTTPException(status_code=403, detail="查無此資料或無權存取")
        if item.status != "pending":
            raise HTTPException(
                status_code=400, detail=f"狀態為 {item.status}，無法取消"
            )
        item.status = "cancelled"
        item.updated_at = datetime.now()
        session.commit()
        return {"status": "ok"}
    finally:
        session.close()
