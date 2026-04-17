"""
api/activity/attendance.py — 才藝點名管理（管理端）
"""

import logging
from datetime import date
from io import BytesIO
from typing import List, Optional

import openpyxl
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, case
from sqlalchemy.exc import IntegrityError

from models.database import get_session
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivityAttendance,
    RegistrationCourse,
)
from utils.auth import get_current_user, require_staff_permission
from utils.permissions import Permission
from api.activity._shared import _build_session_detail_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attendance")


# ── Pydantic Schemas ──────────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    course_id: int
    session_date: date
    notes: Optional[str] = None


class AttendanceRecordItem(BaseModel):
    registration_id: int
    is_present: bool
    notes: Optional[str] = ""


class BatchAttendanceUpdate(BaseModel):
    records: List[AttendanceRecordItem] = Field(..., min_length=1, max_length=500)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/sessions",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_READ))],
)
def list_sessions(
    course_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
):
    """場次列表（可依課程、日期範圍篩選，支援分頁）"""
    session = get_session()
    try:
        query = session.query(
            ActivitySession.id,
            ActivitySession.course_id,
            ActivitySession.session_date,
            ActivitySession.notes,
            ActivitySession.created_by,
            ActivitySession.created_at,
            ActivityCourse.name.label("course_name"),
        ).join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
        if course_id:
            query = query.filter(ActivitySession.course_id == course_id)
        if start_date:
            query = query.filter(ActivitySession.session_date >= start_date)
        if end_date:
            query = query.filter(ActivitySession.session_date <= end_date)
        total = query.count()
        rows = (
            query.order_by(
                ActivitySession.session_date.desc(), ActivitySession.id.desc()
            )
            .offset(skip)
            .limit(limit)
            .all()
        )

        # 計算各場次出席統計（SQL GROUP BY，避免 Python 迴圈）
        session_ids = [r.id for r in rows]
        attendance_stats: dict[int, dict] = {}
        if session_ids:
            agg_rows = (
                session.query(
                    ActivityAttendance.session_id,
                    func.count(ActivityAttendance.id).label("recorded"),
                    func.sum(
                        case((ActivityAttendance.is_present.is_(True), 1), else_=0)
                    ).label("present"),
                )
                .filter(ActivityAttendance.session_id.in_(session_ids))
                .group_by(ActivityAttendance.session_id)
                .all()
            )
            attendance_stats = {
                row.session_id: {"recorded": row.recorded, "present": row.present or 0}
                for row in agg_rows
            }

        result = []
        for r in rows:
            stat = attendance_stats.get(r.id, {"recorded": 0, "present": 0})
            result.append(
                {
                    "id": r.id,
                    "course_id": r.course_id,
                    "course_name": r.course_name,
                    "session_date": (
                        r.session_date.isoformat() if r.session_date else None
                    ),
                    "notes": r.notes or "",
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "recorded_count": stat["recorded"],
                    "present_count": stat["present"],
                }
            )
        return {"items": result, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


@router.post(
    "/sessions",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_WRITE))],
)
def create_session(
    body: SessionCreate,
    current_user: dict = Depends(get_current_user),
):
    """建立場次（同課程同日重複則 400）"""
    session = get_session()
    try:
        course = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.id == body.course_id)
            .first()
        )
        if not course:
            raise HTTPException(status_code=404, detail="找不到課程")

        sess = ActivitySession(
            course_id=body.course_id,
            session_date=body.session_date,
            notes=body.notes,
            created_by=current_user.get("username"),
        )
        session.add(sess)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=400, detail="該課程在此日期已有場次")
        session.refresh(sess)
        return {
            "id": sess.id,
            "course_id": sess.course_id,
            "course_name": course.name,
            "session_date": sess.session_date.isoformat(),
            "notes": sess.notes or "",
            "created_by": sess.created_by,
            "created_at": sess.created_at.isoformat() if sess.created_at else None,
        }
    finally:
        session.close()


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_WRITE))],
)
def delete_session(
    session_id: int,
    current_user: dict = Depends(get_current_user),
):
    """刪除場次及所有點名記錄"""
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")
        session.delete(sess)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.get(
    "/sessions/{session_id}",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_READ))],
)
def get_session_detail(
    session_id: int,
    group_by: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """場次詳情 + 已報名學生出席狀態。

    group_by="classroom" → 額外回傳 groups：按班級分組（未分班歸「未分班」末尾）。
    """
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")
        group_key = "classroom" if group_by == "classroom" else None
        return _build_session_detail_response(session, sess, group_by=group_key)
    finally:
        session.close()


@router.get(
    "/sessions/{session_id}/export",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_READ))],
)
def export_session_attendance(
    session_id: int,
    current_user: dict = Depends(get_current_user),
):
    """匯出場次點名記錄（Excel）"""
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")

        data = _build_session_detail_response(session, sess)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "點名記錄"
        ws.append(["姓名", "班級", "出席狀態", "備註"])

        status_map = {True: "出席", False: "缺席", None: "未點名"}
        for s in data["students"]:
            ws.append(
                [
                    s["student_name"],
                    s["class_name"],
                    status_map.get(s["is_present"], "未點名"),
                    s["attendance_notes"],
                ]
            )

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"點名_{data['course_name']}_{data['session_date']}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
    finally:
        session.close()


@router.put(
    "/sessions/{session_id}/records",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_WRITE))],
)
def batch_update_attendance(
    session_id: int,
    body: BatchAttendanceUpdate,
    current_user: dict = Depends(get_current_user),
):
    """批次儲存點名記錄（upsert）"""
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")

        operator = current_user.get("username")

        # 批次查詢現有記錄，避免 N+1
        req_reg_ids = [item.registration_id for item in body.records]
        existing_map = {
            a.registration_id: a
            for a in session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == session_id,
                ActivityAttendance.registration_id.in_(req_reg_ids),
            )
            .all()
        }

        # 批次查詢 registration → student_id，供新建 attendance 寫入冗餘欄位
        reg_student_map = (
            dict(
                session.query(ActivityRegistration.id, ActivityRegistration.student_id)
                .filter(ActivityRegistration.id.in_(req_reg_ids))
                .all()
            )
            if req_reg_ids
            else {}
        )

        for item in body.records:
            existing = existing_map.get(item.registration_id)
            if existing:
                existing.is_present = item.is_present
                existing.notes = item.notes or ""
                existing.recorded_by = operator
                # 若舊 attendance 尚未帶 student_id（backfill 前建立的），補齊
                if existing.student_id is None:
                    existing.student_id = reg_student_map.get(item.registration_id)
            else:
                att = ActivityAttendance(
                    session_id=session_id,
                    registration_id=item.registration_id,
                    student_id=reg_student_map.get(item.registration_id),
                    is_present=item.is_present,
                    notes=item.notes or "",
                    recorded_by=operator,
                )
                session.add(att)

        session.commit()
        return {"ok": True, "updated": len(body.records)}
    finally:
        session.close()
