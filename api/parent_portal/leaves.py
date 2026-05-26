"""api/parent_portal/leaves.py — 家長端學生請假申請（自動核准）。

- POST /api/parent/student-leaves（提交即 status=approved 並寫 attendance）
- GET  /api/parent/student-leaves（列出家長所有小孩的申請）
- GET  /api/parent/student-leaves/{id}
- POST /api/parent/student-leaves/{id}/cancel（僅 status=approved 且
  start_date > today 可取消，並反向清除 attendance）

期間規則：
- start_date 不可早於今天前 30 天，不可晚於今天後 60 天
- end_date 必 >= start_date
- 同一 student 在 start_date..end_date 區間內若有 approved 重疊
  → 400（避免 attendance 寫入衝突）
"""

import logging
import os
from datetime import date, datetime, timedelta
from utils.taipei_time import now_taipei_naive
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.database import (
    Attachment,
    Guardian,
    StudentLeaveRequest,
    StudentAttendance,
)
from models.portfolio import ATTACHMENT_OWNER_STUDENT_LEAVE
from models.student_leave import LEAVE_TYPES
from services.student_leave_service import (
    apply_attendance_for_leave,
    revert_attendance_for_leave,
)
from utils.auth import require_parent_role
from utils.file_upload import (
    read_upload_with_size_check,
    safe_attachment_filename,
    validate_file_signature,
)
from utils.portfolio_storage import (
    heic_supported,
    is_heic_extension,
)

from ._dependencies import get_parent_db
from ._shared import _assert_student_owned, _get_parent_student_ids

logger = logging.getLogger(__name__)

# 病假診斷證明 / 事假佐證附件白名單
_PARENT_LEAVE_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf"}

router = APIRouter(prefix="/student-leaves", tags=["parent-leaves"])


_PAST_LIMIT_DAYS = 30
_FUTURE_LIMIT_DAYS = 60


class CreateLeaveRequest(BaseModel):
    student_id: int = Field(..., gt=0)
    leave_type: str = Field(...)
    start_date: date
    end_date: date
    reason: Optional[str] = Field(None, max_length=500)
    client_request_id: Optional[str] = Field(
        None,
        max_length=64,
        description="前端產生的 UUID，partial UNIQUE 提供冪等性",
    )

    @field_validator("leave_type")
    @classmethod
    def _check_type(cls, v):
        if v not in LEAVE_TYPES:
            raise ValueError(f"leave_type 須為 {LEAVE_TYPES} 之一")
        return v


def _validate_date_range(req: CreateLeaveRequest) -> None:
    today = date.today()  # noqa: DTZ011
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
            StudentLeaveRequest.status == "approved",
            StudentLeaveRequest.start_date <= end,
            StudentLeaveRequest.end_date >= start,
        )
        .first()
    )
    if overlap is not None:
        raise HTTPException(
            status_code=400,
            detail="此期間已有其他已成立的請假，請調整日期或聯絡老師",
        )


def _attachment_to_dict(att: Attachment) -> dict:
    return {
        "id": att.id,
        "storage_key": att.storage_key,
        "display_key": att.display_key,
        "thumb_key": att.thumb_key,
        "original_filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "uploaded_at": att.created_at.isoformat() if att.created_at else None,
    }


def _load_leave_attachments(session, leave_id: int) -> list[dict]:
    rows = (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_STUDENT_LEAVE,
            Attachment.owner_id == leave_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.id.asc())
        .all()
    )
    return [_attachment_to_dict(a) for a in rows]


def _serialize(
    item: StudentLeaveRequest, attachments: Optional[list[dict]] = None
) -> dict:
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
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "attachments": attachments if attachments is not None else [],
    }


@router.post("", status_code=201)
def create_leave(
    payload: CreateLeaveRequest,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]

    # IDOR 守衛必須在冪等 pre-check 之前執行，避免跨家庭洩漏 UUID 存在性
    _assert_student_owned(session, user_id, payload.student_id, for_write=True)

    # 冪等 pre-check：同 client_request_id 已存在則回原請假（跳過 validation + insert）
    if payload.client_request_id:
        existing = session.scalar(
            select(StudentLeaveRequest).where(
                StudentLeaveRequest.client_request_id == payload.client_request_id
            )
        )
        if existing is not None:
            request.state.audit_summary = (
                f"家長申請請假 idempotent replay：leave={existing.id}"
            )
            return _serialize(existing, _load_leave_attachments(session, existing.id))

    # 既有 validation（replay 路徑跳過）
    _validate_date_range(payload)
    _check_overlap(session, payload.student_id, payload.start_date, payload.end_date)

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
        status="approved",
        reviewed_at=now_taipei_naive(),
        reviewed_by=None,
        client_request_id=payload.client_request_id,
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError as e:
        # PG partial UNIQUE 競爭寫入（23505）防護；SQLite 測試不觸發此路徑
        if "ix_student_leave_requests_client_request_id" in str(e.orig):
            session.rollback()
            existing = session.scalar(
                select(StudentLeaveRequest).where(
                    StudentLeaveRequest.client_request_id == payload.client_request_id
                )
            )
            if existing:
                request.state.audit_summary = (
                    f"家長申請請假 idempotent 23505 replay：leave={existing.id}"
                )
                return _serialize(
                    existing, _load_leave_attachments(session, existing.id)
                )
        raise
    apply_attendance_for_leave(session, item, recorded_by=None)
    # NOTE: dep owns the commit — handler must NOT call session.commit() or
    # SET LOCAL app.current_user_id is lost and subsequent queries get 0 rows.
    session.flush()
    session.refresh(item)
    request.state.audit_entity_id = str(item.id)
    request.state.audit_summary = (
        f"家長提交請假：leave_id={item.id} student_id={item.student_id} "
        f"period={item.start_date}~{item.end_date} type={item.leave_type}"
    )
    logger.info(
        "家長提交請假：leave_id=%d student_id=%d parent_user=%d type=%s",
        item.id,
        item.student_id,
        user_id,
        item.leave_type,
    )
    return _serialize(item)


@router.get("")
def list_leaves(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
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


@router.get("/{leave_id}")
def get_leave(
    leave_id: int,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
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
    attachments = _load_leave_attachments(session, item.id)
    return _serialize(item, attachments)


@router.post("/{leave_id}/attachments", status_code=201)
async def upload_leave_attachment(
    leave_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """為已建立的請假申請上傳佐證檔案（診斷證明、活動行程等）。

    僅在 status='approved' 且 start_date > 今天時允許上傳；請假已開始或已取消後
    不再受理變更。若需補件，請聯絡老師。
    """
    user_id = current_user["user_id"]

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _PARENT_LEAVE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；接受 JPG/PNG/HEIC/PDF",
        )
    if is_heic_extension(ext) and not heic_supported():
        raise HTTPException(
            status_code=400,
            detail="伺服器未安裝 HEIC 解碼套件，請改傳 JPG/PNG",
        )
    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    from utils.portfolio_storage import get_portfolio_storage

    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    item = (
        session.query(StudentLeaveRequest)
        .filter(StudentLeaveRequest.id == leave_id)
        .first()
    )
    if item is None or item.student_id not in owned_student_ids:
        raise HTTPException(status_code=403, detail="查無此資料或無權存取")
    today = date.today()  # noqa: DTZ011
    if not (item.status == "approved" and item.start_date > today):
        raise HTTPException(
            status_code=400,
            detail="請假已成立或已開始，無法新增/刪除附件",
        )

    storage = get_portfolio_storage()
    stored = storage.put_attachment(content, ext)

    # P1-9：sanitize 後再入庫，與 medications / events 對齊。
    safe_name = safe_attachment_filename(filename, ext)
    att = Attachment(
        owner_type=ATTACHMENT_OWNER_STUDENT_LEAVE,
        owner_id=item.id,
        storage_key=stored.storage_key,
        display_key=stored.display_key,
        thumb_key=stored.thumb_key,
        original_filename=safe_name,
        mime_type=stored.mime_type,
        size_bytes=len(content),
        uploaded_by=user_id,
    )
    session.add(att)
    session.flush()
    session.refresh(att)

    request.state.audit_entity_id = str(item.id)
    request.state.audit_summary = (
        f"家長上傳請假附件：leave_id={item.id} "
        f"attachment_id={att.id} filename={filename} size={len(content)}B"
    )
    logger.info(
        "家長上傳請假附件：leave_id=%d attachment_id=%d size=%d parent_user=%d",
        item.id,
        att.id,
        len(content),
        user_id,
    )
    return _attachment_to_dict(att)


@router.delete("/{leave_id}/attachments/{attachment_id}")
def delete_leave_attachment(
    leave_id: int,
    attachment_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """軟刪除請假附件；同樣僅 status='approved' 且 start_date > 今天時可刪。"""
    user_id = current_user["user_id"]
    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    item = (
        session.query(StudentLeaveRequest)
        .filter(StudentLeaveRequest.id == leave_id)
        .first()
    )
    if item is None or item.student_id not in owned_student_ids:
        raise HTTPException(status_code=403, detail="查無此資料或無權存取")
    today = date.today()  # noqa: DTZ011
    if not (item.status == "approved" and item.start_date > today):
        raise HTTPException(
            status_code=400,
            detail="請假已成立或已開始，無法新增/刪除附件",
        )

    att = (
        session.query(Attachment)
        .filter(
            Attachment.id == attachment_id,
            Attachment.owner_type == ATTACHMENT_OWNER_STUDENT_LEAVE,
            Attachment.owner_id == item.id,
            Attachment.deleted_at.is_(None),
        )
        .first()
    )
    if not att:
        raise HTTPException(status_code=404, detail="附件不存在")
    att.deleted_at = now_taipei_naive()
    session.flush()

    request.state.audit_entity_id = str(item.id)
    request.state.audit_summary = (
        f"家長刪除請假附件：leave_id={item.id} attachment_id={att.id}"
    )
    return {"status": "ok"}


@router.post("/{leave_id}/cancel")
def cancel_leave(
    leave_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """僅 status='approved' 且 start_date > today 可取消，並反向清除 attendance。"""
    user_id = current_user["user_id"]
    today = date.today()  # noqa: DTZ011
    # F-004：「申請不存在」與「不屬於本家庭」collapse 為單一 403。
    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    item = (
        session.query(StudentLeaveRequest)
        .filter(StudentLeaveRequest.id == leave_id)
        .first()
    )
    if item is None or item.student_id not in owned_student_ids:
        raise HTTPException(status_code=403, detail="查無此資料或無權存取")
    if item.status != "approved":
        raise HTTPException(status_code=400, detail=f"狀態為 {item.status}，無法取消")
    if item.start_date <= today:
        raise HTTPException(status_code=400, detail="請假期間已開始，無法取消")
    affected = revert_attendance_for_leave(session, item)
    item.status = "cancelled"
    item.updated_at = now_taipei_naive()
    session.flush()
    request.state.audit_entity_id = str(item.id)
    request.state.audit_summary = (
        f"家長取消請假：leave_id={item.id} "
        f"period={item.start_date}~{item.end_date} reverted_attendance={affected}"
    )
    logger.info(
        "家長取消請假：leave_id=%d reverted_attendance=%d parent_user=%d",
        item.id,
        affected,
        user_id,
    )
    return {"status": "ok"}
