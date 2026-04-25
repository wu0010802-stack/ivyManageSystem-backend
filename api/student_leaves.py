"""api/student_leaves.py — 教師審核家長端學生請假申請。

- GET  /api/student-leaves?status=&classroom_id=（列出待審）
- POST /api/student-leaves/{id}/approve
- POST /api/student-leaves/{id}/reject

approve 規則（plan A.4）：
- 同 transaction 對 compute_attendance_dates 回傳的每個應到日 upsert
  StudentAttendance（status=leave_type, remark=家長申請#<id>,
  recorded_by=reviewer.id）
- 衝突時 approval wins：覆蓋 status，但保留原 recorded_by

reject / cancel 反向清除：僅清 remark 前綴吻合者（保留教師後手寫的紀錄）。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from models.database import (
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
    get_session,
)
from services.student_leave_service import (
    REMARK_PREFIX,
    compute_attendance_dates,
    is_remark_owned_by_leave,
    make_remark,
)
from services.workday_rules import load_day_rule_maps
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/student-leaves", tags=["student-leaves"])

_line_service = None


def init_student_leaves_line_service(line_service) -> None:
    """注入 LineService（main.py 啟動時呼叫一次）。未注入時推播靜默 noop。"""
    global _line_service
    _line_service = line_service


def _notify_parent_leave_result_safe(session, item: StudentLeaveRequest, approved: bool) -> None:
    """fail-safe 通知家長審核結果；任何失敗都僅 log，不影響審核 transaction。"""
    if _line_service is None:
        return
    try:
        applicant = (
            session.query(User).filter(User.id == item.applicant_user_id).first()
        )
        if applicant is None or not applicant.line_user_id:
            return
        student = session.query(Student).filter(Student.id == item.student_id).first()
        student_name = student.name if student else "您的小孩"
        _line_service.notify_parent_leave_result(
            applicant.line_user_id,
            student_name,
            item.leave_type,
            item.start_date,
            item.end_date,
            approved=approved,
            review_note=item.review_note,
        )
    except Exception:
        logger.warning("家長端請假審核 LINE 推播失敗（已忽略）", exc_info=True)


class ReviewPayload(BaseModel):
    review_note: Optional[str] = Field(None, max_length=500)


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
def list_pending_leaves(
    status: str = Query("pending"),
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
        if classroom_id is not None:
            q = q.filter(Student.classroom_id == classroom_id)
        rows = (
            q.order_by(StudentLeaveRequest.created_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "items": [
                _serialize(item, student_name=student.name) for item, student in rows
            ],
            "total": len(rows),
        }
    finally:
        session.close()


def _apply_attendance_for_leave(
    session, leave: StudentLeaveRequest, reviewer_user_id: int
) -> int:
    """在當前 session 內（必由 caller 開的 transaction）upsert StudentAttendance。
    回傳實際被建立或覆蓋的天數。
    """
    holiday_map, makeup_map = load_day_rule_maps(session, leave.start_date, leave.end_date)
    dates = compute_attendance_dates(
        leave.start_date, leave.end_date, holiday_map, makeup_map
    )
    new_remark = make_remark(leave.id)
    affected = 0
    for d in dates:
        existing = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id == leave.student_id,
                StudentAttendance.date == d,
            )
            .first()
        )
        if existing is None:
            session.add(
                StudentAttendance(
                    student_id=leave.student_id,
                    date=d,
                    status=leave.leave_type,
                    remark=new_remark,
                    recorded_by=reviewer_user_id,
                )
            )
        else:
            existing.status = leave.leave_type
            existing.remark = new_remark
            # recorded_by 不覆蓋（保留原作者）
        affected += 1
    return affected


def _revert_attendance_for_leave(session, leave: StudentLeaveRequest) -> int:
    """反向清除 approve 時寫的紀錄；僅刪除 remark 吻合的紀錄（保留教師後手紀錄）。"""
    rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id == leave.student_id,
            StudentAttendance.date >= leave.start_date,
            StudentAttendance.date <= leave.end_date,
        )
        .all()
    )
    affected = 0
    for r in rows:
        if is_remark_owned_by_leave(r.remark, leave.id):
            session.delete(r)
            affected += 1
    return affected


@router.post("/{leave_id}/approve")
def approve_leave(
    leave_id: int,
    payload: ReviewPayload = ReviewPayload(),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    session = get_session()
    try:
        item = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.id == leave_id)
            .first()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="找不到申請")
        if item.status != "pending":
            raise HTTPException(
                status_code=400, detail=f"狀態為 {item.status}，僅 pending 可審核"
            )

        item.status = "approved"
        item.reviewed_by = current_user["user_id"]
        item.reviewed_at = datetime.now()
        item.review_note = (payload.review_note or "").strip() or None
        affected = _apply_attendance_for_leave(
            session, item, reviewer_user_id=current_user["user_id"]
        )
        session.commit()
        _notify_parent_leave_result_safe(session, item, approved=True)
        return {"status": "ok", "affected_days": affected}
    finally:
        session.close()


@router.post("/{leave_id}/reject")
def reject_leave(
    leave_id: int,
    payload: ReviewPayload = ReviewPayload(),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    session = get_session()
    try:
        item = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.id == leave_id)
            .first()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="找不到申請")
        if item.status not in ("pending", "approved"):
            raise HTTPException(
                status_code=400, detail=f"狀態為 {item.status}，無法駁回"
            )

        # 若先前已 approved，需反向清除 attendance
        if item.status == "approved":
            _revert_attendance_for_leave(session, item)

        item.status = "rejected"
        item.reviewed_by = current_user["user_id"]
        item.reviewed_at = datetime.now()
        item.review_note = (payload.review_note or "").strip() or None
        session.commit()
        _notify_parent_leave_result_safe(session, item, approved=False)
        return {"status": "ok"}
    finally:
        session.close()
