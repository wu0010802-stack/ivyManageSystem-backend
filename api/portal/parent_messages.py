"""api/portal/parent_messages.py — 教師端家園溝通（Phase 3）

教師端可主動發起 thread（限班導師對自己班級學生家長），可回覆既有 thread。

端點：
- GET    /api/portal/parent-messages/threads
- GET    /api/portal/parent-messages/threads/{id}
- GET    /api/portal/parent-messages/threads/{id}/messages
- POST   /api/portal/parent-messages/threads                                  發起新 thread + 第一則訊息
- POST   /api/portal/parent-messages/threads/{id}/messages                    教師回覆
- POST   /api/portal/parent-messages/threads/{id}/messages/{mid}/attach       多 part 上傳附件
- POST   /api/portal/parent-messages/threads/{id}/read                        標已讀
- POST   /api/portal/parent-messages/messages/{mid}/recall                    30 分內撤回
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from models.database import (
    Attachment,
    Guardian,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
    get_session,
)
from models.portfolio import ATTACHMENT_OWNER_MESSAGE
from services.parent_message_service import (
    append_message,
    assert_teacher_is_homeroom,
    assert_thread_participant,
    can_recall,
    count_unread_for_teacher,
    get_or_create_thread,
    mark_read,
)
from utils.auth import require_permission
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from utils.permissions import Permission

logger = logging.getLogger(__name__)

# LINE 推播服務（main.py 啟動時注入；未注入時 push 變 no-op）
_line_service = None


def init_parent_messages_line_service(svc) -> None:
    global _line_service
    _line_service = svc


def _push_parent_message_received(
    *,
    parent_user_id: int,
    teacher_name: str,
    student_name: Optional[str],
    body_preview: str,
    thread_id: Optional[int] = None,
) -> None:
    """非阻塞地推播；自管 session（與 endpoint 的 session 解耦）。

    thread_id 傳入時 LINE 推播會附 quick-reply postback，家長可直接回覆。
    """
    if _line_service is None:
        return
    try:
        s = get_session()
        try:
            _line_service.notify_parent_message_received(
                s,
                parent_user_id=parent_user_id,
                teacher_name=teacher_name,
                student_name=student_name,
                body_preview=body_preview,
                thread_id=thread_id,
            )
        finally:
            s.close()
    except Exception as exc:
        logger.warning("notify_parent_message_received 失敗（已吞）：%s", exc)


router = APIRouter(prefix="/parent-messages", tags=["portal-parent-messages"])

_MSG_ATTACH_ALLOWED_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".heic",
    ".heif",
    ".pdf",
}


class CreateThreadRequest(BaseModel):
    student_id: int = Field(..., gt=0)
    parent_user_id: int = Field(..., gt=0)
    body: str = Field(..., min_length=1, max_length=4000)
    client_request_id: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class TeacherReplyRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)
    client_request_id: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


# ── helpers ──────────────────────────────────────────────────────────────


def _attachment_url_for_staff(att: Attachment, kind: str) -> Optional[str]:
    key = (
        att.storage_key
        if kind == "storage"
        else (att.thumb_key if kind == "thumb" else att.display_key)
    )
    if not key:
        return None
    return f"/api/uploads/portfolio/{key}"


def _attachments_for_message(session, message_id: int) -> list[Attachment]:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_MESSAGE,
            Attachment.owner_id == message_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.id.asc())
        .all()
    )


def _attachment_to_dict(att: Attachment) -> dict:
    return {
        "id": att.id,
        "original_filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": _attachment_url_for_staff(att, "storage"),
        "thumb_url": _attachment_url_for_staff(att, "thumb"),
        "display_url": _attachment_url_for_staff(att, "display"),
    }


def _message_to_dict(msg: ParentMessage, atts: list[Attachment]) -> dict:
    return {
        "id": msg.id,
        "thread_id": msg.thread_id,
        "sender_user_id": msg.sender_user_id,
        "sender_role": msg.sender_role,
        "body": msg.body if msg.deleted_at is None else None,
        "deleted": msg.deleted_at is not None,
        "source": msg.source,
        "client_request_id": msg.client_request_id,
        "attachments": [_attachment_to_dict(a) for a in atts],
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def _thread_summary(session, *, t: ParentMessageThread) -> dict:
    student = session.query(Student).filter(Student.id == t.student_id).first()
    parent = session.query(User).filter(User.id == t.parent_user_id).first()
    last = (
        session.query(ParentMessage)
        .filter(ParentMessage.thread_id == t.id)
        .order_by(ParentMessage.created_at.desc())
        .first()
    )
    last_preview = None
    if last and last.deleted_at is None:
        last_preview = (last.body or "(附件)")[:60]
    elif last and last.deleted_at is not None:
        last_preview = "(已撤回)"

    cutoff = t.teacher_last_read_at
    unread_q = session.query(ParentMessage).filter(
        ParentMessage.thread_id == t.id,
        ParentMessage.sender_role == "parent",
        ParentMessage.deleted_at.is_(None),
    )
    if cutoff is not None:
        unread_q = unread_q.filter(ParentMessage.created_at > cutoff)
    unread_count = unread_q.count()

    return {
        "id": t.id,
        "student_id": t.student_id,
        "student_name": student.name if student else None,
        "parent_user_id": t.parent_user_id,
        "parent_name": parent.username if parent else None,
        "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None,
        "last_message_preview": last_preview,
        "unread_count": unread_count,
    }


def _resolve_employee_id(current_user: dict) -> int:
    eid = current_user.get("employee_id")
    if not eid:
        raise HTTPException(status_code=403, detail="此 token 無關聯員工")
    return eid


def _get_thread_for_teacher(
    session, *, user_id: int, thread_id: int
) -> ParentMessageThread:
    t = (
        session.query(ParentMessageThread)
        .filter(
            ParentMessageThread.id == thread_id,
            ParentMessageThread.deleted_at.is_(None),
        )
        .first()
    )
    if not t:
        raise HTTPException(status_code=404, detail="thread 不存在")
    assert_thread_participant(t, user_id=user_id, role="teacher")
    return t


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/unread-count")
def get_teacher_unread_count(
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    """教師端家長訊息未讀總數（跨所有 thread）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        count = count_unread_for_teacher(session, teacher_user_id=user_id)
        request.state.audit_summary = "portal.parent_messages.unread_count"
        return {"unread_count": count}
    finally:
        session.close()


@router.get("/threads")
def list_threads(
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        q = session.query(ParentMessageThread).filter(
            ParentMessageThread.deleted_at.is_(None),
        )
        q = q.filter(ParentMessageThread.teacher_user_id == user_id)
        if cursor:
            q = q.filter(ParentMessageThread.id < cursor)
        threads = (
            q.order_by(
                ParentMessageThread.last_message_at.is_(None).asc(),
                ParentMessageThread.last_message_at.desc(),
                ParentMessageThread.id.desc(),
            )
            .limit(limit + 1)
            .all()
        )
        has_more = len(threads) > limit
        page = threads[:limit]
        items = [_thread_summary(session, t=t) for t in page]
        next_cursor = page[-1].id if has_more and page else None
        return {"items": items, "next_cursor": next_cursor}
    finally:
        session.close()


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: int,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        t = _get_thread_for_teacher(session, user_id=user_id, thread_id=thread_id)
        return _thread_summary(session, t=t)
    finally:
        session.close()


@router.get("/threads/{thread_id}/messages")
def list_messages(
    thread_id: int,
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _get_thread_for_teacher(session, user_id=user_id, thread_id=thread_id)
        q = session.query(ParentMessage).filter(ParentMessage.thread_id == thread_id)
        if cursor:
            q = q.filter(ParentMessage.id < cursor)
        rows = q.order_by(ParentMessage.id.desc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        page = rows[:limit]
        # Phase 8 N+1 修補：列表前一次 IN clause 取所有 messages 的 attachments
        message_ids = [m.id for m in page]
        attachments_by_message: dict[int, list] = {mid: [] for mid in message_ids}
        if message_ids:
            atts = (
                session.query(Attachment)
                .filter(
                    Attachment.owner_type == ATTACHMENT_OWNER_MESSAGE,
                    Attachment.owner_id.in_(message_ids),
                    Attachment.deleted_at.is_(None),
                )
                .order_by(Attachment.id.asc())
                .all()
            )
            for a in atts:
                attachments_by_message.setdefault(a.owner_id, []).append(a)
        items = [
            _message_to_dict(m, attachments_by_message.get(m.id, [])) for m in page
        ]
        next_cursor = page[-1].id if has_more and page else None
        return {"items": items, "next_cursor": next_cursor}
    finally:
        session.close()


@router.post("/threads", status_code=201)
def create_thread(
    payload: CreateThreadRequest,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    """教師主動發起新 thread + 第一則訊息。

    限制：班導師可對自己 classroom 學生的家長發起；admin 例外不受班導限制。
    parent_user_id 必須是該 student 的 active guardian.user_id（防發訊到無關 user）。
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # 1. 班導師守衛：嚴格限制（業主決策；admin 也走同一條路）
        employee_id = _resolve_employee_id(current_user)
        assert_teacher_is_homeroom(
            session, employee_id=employee_id, student_id=payload.student_id
        )

        # 2. parent_user 必須是該 student 的 active guardian
        guardian = (
            session.query(Guardian)
            .filter(
                Guardian.student_id == payload.student_id,
                Guardian.user_id == payload.parent_user_id,
                Guardian.deleted_at.is_(None),
            )
            .first()
        )
        if not guardian:
            raise HTTPException(status_code=400, detail="此家長不是該學生的監護人")

        # 3. upsert thread
        thread = get_or_create_thread(
            session,
            parent_user_id=payload.parent_user_id,
            teacher_user_id=user_id,
            student_id=payload.student_id,
        )

        # 4. 寫第一則訊息
        msg, replayed = append_message(
            session,
            thread=thread,
            sender_user_id=user_id,
            sender_role="teacher",
            body=payload.body,
            client_request_id=payload.client_request_id,
            source="app",
        )
        session.commit()
        session.refresh(thread)
        session.refresh(msg)

        request.state.audit_entity_id = str(thread.id)
        request.state.audit_summary = (
            f"教師發起家長訊息：student_id={payload.student_id} "
            f"parent_user={payload.parent_user_id} thread_id={thread.id} "
            f"message_id={msg.id} replay={replayed}"
        )
        # 推播：commit 完才推；replay 不重推（家長已收過）
        if not replayed:
            student_obj = (
                session.query(Student).filter(Student.id == thread.student_id).first()
            )
            teacher_obj = session.query(User).filter(User.id == user_id).first()
            _push_parent_message_received(
                parent_user_id=thread.parent_user_id,
                teacher_name=(teacher_obj.username if teacher_obj else "老師"),
                student_name=(student_obj.name if student_obj else None),
                body_preview=payload.body,
                thread_id=thread.id,
            )
        return {
            "thread": _thread_summary(session, t=thread),
            "message": _message_to_dict(msg, _attachments_for_message(session, msg.id)),
            "idempotent_replay": replayed,
        }
    finally:
        session.close()


@router.post("/threads/{thread_id}/messages", status_code=201)
def post_reply(
    thread_id: int,
    payload: TeacherReplyRequest,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        t = _get_thread_for_teacher(session, user_id=user_id, thread_id=thread_id)
        msg, replayed = append_message(
            session,
            thread=t,
            sender_user_id=user_id,
            sender_role="teacher",
            body=payload.body,
            client_request_id=payload.client_request_id,
            source="app",
        )
        session.commit()
        session.refresh(msg)

        request.state.audit_entity_id = str(msg.id)
        request.state.audit_summary = (
            f"教師回覆家長訊息：thread_id={thread_id} message_id={msg.id} "
            f"replay={replayed}"
        )
        if not replayed:
            student_obj = (
                session.query(Student).filter(Student.id == t.student_id).first()
            )
            teacher_obj = session.query(User).filter(User.id == user_id).first()
            _push_parent_message_received(
                parent_user_id=t.parent_user_id,
                teacher_name=(teacher_obj.username if teacher_obj else "老師"),
                student_name=(student_obj.name if student_obj else None),
                body_preview=payload.body,
                thread_id=t.id,
            )
        return {
            **_message_to_dict(msg, _attachments_for_message(session, msg.id)),
            "idempotent_replay": replayed,
        }
    finally:
        session.close()


@router.post("/threads/{thread_id}/messages/{message_id}/attach", status_code=201)
async def attach_to_message(
    thread_id: int,
    message_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _MSG_ATTACH_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；接受 JPG/PNG/HEIC/PDF",
        )
    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    from utils.portfolio_storage import get_portfolio_storage

    session = get_session()
    try:
        t = _get_thread_for_teacher(session, user_id=user_id, thread_id=thread_id)
        msg = (
            session.query(ParentMessage)
            .filter(
                ParentMessage.id == message_id,
                ParentMessage.thread_id == t.id,
            )
            .first()
        )
        if not msg:
            raise HTTPException(status_code=404, detail="訊息不存在")
        if msg.sender_user_id != user_id:
            raise HTTPException(status_code=403, detail="僅可附加在自己的訊息")
        if msg.deleted_at is not None:
            raise HTTPException(status_code=400, detail="已撤回的訊息不可附加附件")

        storage = get_portfolio_storage()
        stored = storage.put_attachment(content, ext)
        att = Attachment(
            owner_type=ATTACHMENT_OWNER_MESSAGE,
            owner_id=msg.id,
            storage_key=stored.storage_key,
            display_key=stored.display_key,
            thumb_key=stored.thumb_key,
            original_filename=filename,
            mime_type=stored.mime_type,
            size_bytes=len(content),
            uploaded_by=user_id,
        )
        session.add(att)
        session.flush()
        session.refresh(att)
        session.commit()

        request.state.audit_entity_id = str(msg.id)
        request.state.audit_summary = (
            f"教師上傳訊息附件：thread_id={thread_id} message_id={msg.id} "
            f"attachment_id={att.id}"
        )
        return _attachment_to_dict(att)
    finally:
        session.close()


@router.post("/threads/{thread_id}/read", status_code=200)
def mark_thread_read(
    thread_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        t = _get_thread_for_teacher(session, user_id=user_id, thread_id=thread_id)
        mark_read(session, thread=t, role="teacher")
        session.commit()
        request.state.audit_summary = "portal.parent_messages.mark_read"
        return {"status": "ok"}
    finally:
        session.close()


@router.post("/messages/{message_id}/recall", status_code=200)
def recall_message(
    message_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PARENT_MESSAGES_WRITE)),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        msg = (
            session.query(ParentMessage).filter(ParentMessage.id == message_id).first()
        )
        if not msg:
            raise HTTPException(status_code=404, detail="訊息不存在")
        if not can_recall(msg, user_id=user_id):
            raise HTTPException(status_code=403, detail="只有 sender 30 分鐘內可撤回")
        msg.deleted_at = datetime.now()
        session.commit()
        request.state.audit_entity_id = str(msg.id)
        request.state.audit_summary = (
            f"教師撤回訊息：thread_id={msg.thread_id} message_id={msg.id}"
        )
        return {"status": "ok", "deleted_at": msg.deleted_at.isoformat()}
    finally:
        session.close()
