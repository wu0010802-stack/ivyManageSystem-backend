"""
api/activity/attendance.py — 才藝點名管理（管理端）
"""

import logging
from datetime import date
from io import BytesIO
from typing import List, Optional
from urllib.parse import quote

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models.database import get_session
from models.activity import (
    ActivityCourse,
    ActivitySession,
    ActivityAttendance,
)
from utils.audit import write_explicit_audit
from utils.auth import get_current_user, require_staff_permission
from utils.excel_utils import SafeWorksheet
from utils.permissions import Permission
from api.activity._shared import (
    _build_session_detail_response,
    build_session_rows_with_stats,
    query_valid_session_registrations,
    resolve_student_pii_scope,
)
from services.activity_attendance_roll_pdf import generate_attendance_roll_pdf
from schemas.activity_admin import (
    ActivityAttendanceBatchUpdateResultOut,
    ActivitySessionCreateResultOut,
    ActivitySessionDeleteResultOut,
    ActivitySessionDetailOut,
    ActivitySessionListOut,
)

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
    response_model=ActivitySessionListOut,
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_READ))],
)
def list_sessions(
    course_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    # 裸 int 時 skip=-1 在 PG OFFSET 直接 500、limit 無上限可全表 dump；
    # 對齊同 package 其他列表端點（courses/supplies）的 Query 驗證慣例
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
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

        result = build_session_rows_with_stats(session, rows)
        return {"items": result, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


@router.post(
    "/sessions",
    response_model=ActivitySessionCreateResultOut,
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_WRITE))],
)
def create_session(
    body: SessionCreate,
    current_user: dict = Depends(get_current_user),
):
    """建立場次（同課程同日重複則 400）

    課程已停用（is_active=False）視同不存在，回 404；
    避免為退場的課程繼續建場次造成統計與點名異常。
    """
    session = get_session()
    try:
        course = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == body.course_id,
                ActivityCourse.is_active.is_(True),
            )
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
    response_model=ActivitySessionDeleteResultOut,
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_WRITE))],
)
def delete_session(
    session_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """刪除場次及所有點名記錄（顯式留稽核）

    硬刪場次會 CASCADE 抹除該場所有點名紀錄；AuditMiddleware 的
    ENTITY_PATTERNS 不涵蓋 /api/activity/attendance/* 路徑，故在此
    顯式落 audit_logs（誰、哪課、哪日、抹掉幾筆出席紀錄），對齊
    退課路徑「同步清除 N 筆舊點名紀錄」的稽核慣例。
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

        # 刪除前先快照稽核素材（刪後 CASCADE 就查不到了）
        course_name = (
            session.query(ActivityCourse.name)
            .filter(ActivityCourse.id == sess.course_id)
            .scalar()
        ) or ""
        removed_attendance = (
            session.query(func.count(ActivityAttendance.id))
            .filter(ActivityAttendance.session_id == session_id)
            .scalar()
        ) or 0
        course_id = sess.course_id
        session_date = sess.session_date.isoformat()
        operator = current_user.get("username", "")

        session.delete(sess)
        session.commit()

        write_explicit_audit(
            request,
            action="DELETE",
            entity_type="activity_session",
            entity_id=str(session_id),
            summary=(
                f"刪除才藝場次：「{course_name}」{session_date}"
                f"（連帶抹除 {removed_attendance} 筆點名紀錄）"
            ),
            changes={
                "course_id": course_id,
                "course_name": course_name,
                "session_date": session_date,
                "removed_attendance": removed_attendance,
                "operator": operator,
            },
        )
        return {"ok": True}
    finally:
        session.close()


@router.get(
    "/sessions/{session_id}",
    response_model=ActivitySessionDetailOut,
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
        # S6/S7：僅 ACTIVITY_READ 的 caller 不應拿到學生/班級 FK（對齊
        # registrations_pending F-026 慣例）；STUDENTS_READ:own_class 者
        # 對非管轄班級的列照樣遮罩（scope-aware）
        pii_visible, pii_allowed = resolve_student_pii_scope(session, current_user)
        return _build_session_detail_response(
            session,
            sess,
            group_by=group_key,
            mask_student_ids=not pii_visible,
            student_pii_visible_classroom_ids=pii_allowed,
        )
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
        ws = SafeWorksheet(wb.active)
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


@router.get(
    "/sessions/{session_id}/roll.pdf",
    dependencies=[Depends(require_staff_permission(Permission.ACTIVITY_READ))],
)
def print_session_roll_pdf(
    session_id: int,
    current_user: dict = Depends(get_current_user),
):
    """產生場次點名單 PDF（瀏覽器原生 PDF viewer 可直接列印）。"""
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
        pdf_bytes = generate_attendance_roll_pdf(session_data=data)
        filename = f"點名單_{data['course_name']}_{data['session_date']}.pdf"
        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                # inline → 瀏覽器直接顯示而非下載；filename* 需 RFC 5987 URL-encode
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
                # 點名狀態會即時變動，禁止任何層的快取
                "Cache-Control": "no-store",
            },
        )
    finally:
        session.close()


@router.put(
    "/sessions/{session_id}/records",
    response_model=ActivityAttendanceBatchUpdateResultOut,
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

        # P2-6：同一 registration_id 在 body 出現多次時去重保留最後一筆，避免對
        # 「本場次尚無紀錄」的同一 reg 兩次 session.add 撞 unique 約束
        # （uq_activity_attendance_session_reg）→ IntegrityError 500、整批點名漏存。
        records = list({item.registration_id: item for item in body.records}.values())

        # 批次查詢現有記錄，避免 N+1
        req_reg_ids = [item.registration_id for item in records]
        existing_map = {
            a.registration_id: a
            for a in session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == session_id,
                ActivityAttendance.registration_id.in_(req_reg_ids),
            )
            .all()
        }

        # 過濾已退課（is_active=False）或已駁回（match_status='rejected'）的報名，
        # 並要求 registration 必須真的報了本 session 對應的課程（enrolled 或
        # promoted_pending 皆算佔位）；避免操作員為「未報該課」的學生寫出席紀錄
        # 污染統計與 student_id 冗餘欄位。一併取 student_id 供冗餘欄位使用。
        valid_reg_rows = query_valid_session_registrations(
            session, sess.course_id, req_reg_ids
        )
        valid_reg_ids = {row[0] for row in valid_reg_rows}
        reg_student_map = dict(valid_reg_rows)

        skipped = [rid for rid in req_reg_ids if rid not in valid_reg_ids]
        if skipped:
            logger.warning(
                "batch_update_attendance skipped invalid registrations: session=%s ids=%s",
                session_id,
                skipped,
            )

        for item in records:
            if item.registration_id not in valid_reg_ids:
                continue
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
        applied = sum(1 for item in records if item.registration_id in valid_reg_ids)
        return {"ok": True, "updated": applied, "skipped": len(skipped)}
    finally:
        session.close()
