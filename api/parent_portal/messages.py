"""api/parent_portal/messages.py — 家長端訊息（Phase 3）

Thread 由教師端先發起；家長僅能讀取與回覆既有 thread（不允許主動建 thread）。

端點：
- GET    /api/parent/messages/threads                              thread 列表
- GET    /api/parent/messages/threads/{id}                         thread 詳情
- GET    /api/parent/messages/threads/{id}/messages                訊息分頁（倒序）
- POST   /api/parent/messages/threads/{id}/messages                家長回覆
- POST   /api/parent/messages/threads/{id}/messages/{mid}/attach   多 part 上傳訊息附件
- POST   /api/parent/messages/threads/{id}/read                    標已讀（更新 parent_last_read_at）
- POST   /api/parent/messages/messages/{mid}/recall                30 分內撤回
- GET    /api/parent/messages/unread-count                         未讀計數
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.database import (
    Attachment,
    Employee,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
)
from models.portfolio import ATTACHMENT_OWNER_MESSAGE
from schemas._common import OkStatusOut, UnreadCountOut
from schemas.parent_portal_messages import (
    MessageRecallOut,
    ParentPortalMessageAttachmentOut,
    ParentPortalMessageListOut,
    ParentPortalMessageReplyOut,
    ParentPortalMessageThreadListOut,
    ParentPortalMessageThreadOut,
)
from services.parent_message_service import (
    append_message,
    assert_thread_participant,
    can_recall,
    count_unread_for_parent,
    mark_read,
)
from utils.auth import require_parent_role
from api.parent_portal._consent_gate import require_current_consent
from utils.file_upload import (
    read_upload_with_size_check,
    safe_attachment_filename,
    validate_file_signature,
)

from ._dependencies import get_parent_db
from services.consent.checker import enforce_student_cross_border

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messages", tags=["parent-messages"])

_MSG_ATTACH_ALLOWED_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".heic",
    ".heif",
    ".pdf",
}


class ReplyMessage(BaseModel):
    body: Optional[str] = Field(default=None, max_length=4000)
    client_request_id: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="冪等鍵：前端產生 UUID；同 thread 內重送回放原訊息",
    )


# ── helpers ──────────────────────────────────────────────────────────────


def _attachment_url_for_parent(
    att: Attachment, key_kind: str = "storage"
) -> str | None:
    key = (
        att.storage_key
        if key_kind == "storage"
        else (att.thumb_key if key_kind == "thumb" else att.display_key)
    )
    if not key:
        return None
    return f"/api/parent/uploads/portfolio/{key}"


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
        "url": _attachment_url_for_parent(att, "storage"),
        "thumb_url": _attachment_url_for_parent(att, "thumb"),
        "display_url": _attachment_url_for_parent(att, "display"),
    }


def _message_to_dict(msg: ParentMessage, attachments: list[Attachment]) -> dict:
    return {
        "id": msg.id,
        "thread_id": msg.thread_id,
        "sender_user_id": msg.sender_user_id,
        "sender_role": msg.sender_role,
        "body": msg.body if msg.deleted_at is None else None,
        "deleted": msg.deleted_at is not None,
        "source": msg.source,
        "client_request_id": msg.client_request_id,
        "attachments": [_attachment_to_dict(a) for a in attachments],
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def _thread_summary_from_maps(
    *,
    thread: ParentMessageThread,
    student: Optional[Student],
    teacher: Optional[User],
    teacher_employee: Optional[Employee],
    last_message: Optional[ParentMessage],
    unread_count: int,
) -> dict:
    """Pure transformer：把預載的 student/teacher/last/unread map 拼成 thread dict。

    teacher_name 解析（資安掃描 2026-05-07 P1）：以 employee.name（員工正式姓名）優先，
    退而求其次 user.display_name；username 是內部登入帳號（emp_xxx 形式），
    不應外洩到家長端。
    """
    last_preview = None
    if last_message and last_message.deleted_at is None:
        last_preview = (last_message.body or "(附件)")[:60]
    elif last_message and last_message.deleted_at is not None:
        last_preview = "(已撤回)"

    teacher_name: Optional[str] = None
    if teacher_employee and teacher_employee.name:
        teacher_name = teacher_employee.name
    elif teacher and teacher.display_name:
        teacher_name = teacher.display_name
    elif teacher:
        teacher_name = "老師"

    return {
        "id": thread.id,
        "student_id": thread.student_id,
        "student_name": student.name if student else None,
        "teacher_user_id": thread.teacher_user_id,
        "teacher_name": teacher_name,
        "last_message_at": (
            thread.last_message_at.isoformat() if thread.last_message_at else None
        ),
        "last_message_preview": last_preview,
        "unread_count": unread_count,
    }


def _thread_summary(
    session, *, thread: ParentMessageThread, parent_user_id: int
) -> dict:
    """單一 thread summary（給 get_thread 等 single-thread 呼叫）；list 端點走 batch 路徑。"""
    student = session.query(Student).filter(Student.id == thread.student_id).first()
    teacher = session.query(User).filter(User.id == thread.teacher_user_id).first()
    teacher_employee: Optional[Employee] = None
    if teacher and teacher.employee_id:
        teacher_employee = (
            session.query(Employee).filter(Employee.id == teacher.employee_id).first()
        )
    last = (
        session.query(ParentMessage)
        .filter(ParentMessage.thread_id == thread.id)
        .order_by(ParentMessage.created_at.desc())
        .first()
    )

    cutoff = thread.parent_last_read_at
    unread_q = session.query(ParentMessage).filter(
        ParentMessage.thread_id == thread.id,
        ParentMessage.sender_role == "teacher",
        ParentMessage.deleted_at.is_(None),
    )
    if cutoff is not None:
        unread_q = unread_q.filter(ParentMessage.created_at > cutoff)
    unread_count = unread_q.count()

    return _thread_summary_from_maps(
        thread=thread,
        student=student,
        teacher=teacher,
        teacher_employee=teacher_employee,
        last_message=last,
        unread_count=unread_count,
    )


def _batch_thread_summaries(
    session,
    *,
    threads: list[ParentMessageThread],
) -> list[dict]:
    """批次組裝 N 個 thread 的 summary，~5 個 SQL round-trip 取代 4N。"""
    if not threads:
        return []

    thread_ids = [t.id for t in threads]
    student_ids = list({t.student_id for t in threads if t.student_id})
    teacher_ids = list({t.teacher_user_id for t in threads if t.teacher_user_id})

    # 1) 學生
    students_by_id: dict[int, Student] = {}
    if student_ids:
        students_by_id = {
            s.id: s
            for s in session.query(Student).filter(Student.id.in_(student_ids)).all()
        }

    # 2) 教師
    teachers_by_id: dict[int, User] = {}
    if teacher_ids:
        teachers_by_id = {
            u.id: u for u in session.query(User).filter(User.id.in_(teacher_ids)).all()
        }

    # 2b) 教師對應的 Employee（for teacher_name 顯示，避免洩漏 username）
    # Refs: 資安掃描 2026-05-07 P1。
    teacher_employee_by_user_id: dict[int, Employee] = {}
    employee_ids = list(
        {u.employee_id for u in teachers_by_id.values() if u.employee_id}
    )
    if employee_ids:
        emp_rows = session.query(Employee).filter(Employee.id.in_(employee_ids)).all()
        emp_by_id = {e.id: e for e in emp_rows}
        teacher_employee_by_user_id = {
            uid: emp_by_id[u.employee_id]
            for uid, u in teachers_by_id.items()
            if u.employee_id and u.employee_id in emp_by_id
        }

    # 3) last message per thread：先以 (thread_id, max(created_at)) 取 key，再 join 撈完整 row
    last_key_subq = (
        session.query(
            ParentMessage.thread_id.label("thread_id"),
            func.max(ParentMessage.created_at).label("max_created_at"),
        )
        .filter(ParentMessage.thread_id.in_(thread_ids))
        .group_by(ParentMessage.thread_id)
        .subquery()
    )
    last_messages = (
        session.query(ParentMessage)
        .join(
            last_key_subq,
            (ParentMessage.thread_id == last_key_subq.c.thread_id)
            & (ParentMessage.created_at == last_key_subq.c.max_created_at),
        )
        .all()
    )
    last_by_thread: dict[int, ParentMessage] = {m.thread_id: m for m in last_messages}

    # 4) unread count per thread：GROUP BY 一次拿
    unread_rows = (
        session.query(ParentMessage.thread_id, func.count(ParentMessage.id))
        .join(
            ParentMessageThread,
            ParentMessage.thread_id == ParentMessageThread.id,
        )
        .filter(
            ParentMessage.thread_id.in_(thread_ids),
            ParentMessage.sender_role == "teacher",
            ParentMessage.deleted_at.is_(None),
            or_(
                ParentMessageThread.parent_last_read_at.is_(None),
                ParentMessage.created_at > ParentMessageThread.parent_last_read_at,
            ),
        )
        .group_by(ParentMessage.thread_id)
        .all()
    )
    unread_by_thread: dict[int, int] = {tid: int(cnt or 0) for tid, cnt in unread_rows}

    return [
        _thread_summary_from_maps(
            thread=t,
            student=students_by_id.get(t.student_id),
            teacher=teachers_by_id.get(t.teacher_user_id),
            teacher_employee=teacher_employee_by_user_id.get(t.teacher_user_id),
            last_message=last_by_thread.get(t.id),
            unread_count=unread_by_thread.get(t.id, 0),
        )
        for t in threads
    ]


def _get_thread_for_parent(
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
    assert_thread_participant(t, user_id=user_id, role="parent")
    return t


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/threads", response_model=ParentPortalMessageThreadListOut)
def list_threads(
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    q = session.query(ParentMessageThread).filter(
        ParentMessageThread.parent_user_id == user_id,
        ParentMessageThread.deleted_at.is_(None),
    )
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
    items = _batch_thread_summaries(session, threads=page)
    next_cursor = page[-1].id if has_more and page else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/threads/{thread_id}", response_model=ParentPortalMessageThreadOut)
def get_thread(
    thread_id: int,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    t = _get_thread_for_parent(session, user_id=user_id, thread_id=thread_id)
    return _thread_summary(session, thread=t, parent_user_id=user_id)


@router.get("/threads/{thread_id}/messages", response_model=ParentPortalMessageListOut)
def list_messages(
    thread_id: int,
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """回傳分頁：新 → 舊；cursor 為 message id（< cursor 的更舊）。"""
    user_id = current_user["user_id"]
    _get_thread_for_parent(session, user_id=user_id, thread_id=thread_id)
    q = session.query(ParentMessage).filter(ParentMessage.thread_id == thread_id)
    if cursor:
        q = q.filter(ParentMessage.id < cursor)
    rows = q.order_by(ParentMessage.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_message_to_dict(m, _attachments_for_message(session, m.id)) for m in page]
    next_cursor = page[-1].id if has_more and page else None
    return {"items": items, "next_cursor": next_cursor}


@router.post(
    "/threads/{thread_id}/messages",
    status_code=201,
    response_model=ParentPortalMessageReplyOut,
)
def post_reply(
    thread_id: int,
    payload: ReplyMessage,
    request: Request,
    current_user: dict = Depends(require_current_consent(write=True)),
    session: Session = Depends(get_parent_db),
):
    """家長回覆既有 thread。沒附件純訊息亦允許 body=None（搭配後續 attach 上傳）。"""
    user_id = current_user["user_id"]
    if not payload.body and not payload.client_request_id:
        # 提早擋：避免空訊息也被 idempotency 抓到
        raise HTTPException(status_code=400, detail="訊息不可為空")
    if not payload.body:
        raise HTTPException(status_code=400, detail="訊息不可為空")

    t = _get_thread_for_parent(session, user_id=user_id, thread_id=thread_id)
    msg, replayed = append_message(
        session,
        thread=t,
        sender_user_id=user_id,
        sender_role="parent",
        body=payload.body,
        client_request_id=payload.client_request_id,
        source="app",
    )
    session.flush()
    session.refresh(msg)

    request.state.audit_entity_id = str(msg.id)
    request.state.audit_summary = (
        f"家長回覆訊息：thread_id={thread_id} message_id={msg.id} " f"replay={replayed}"
    )
    return {
        **_message_to_dict(msg, _attachments_for_message(session, msg.id)),
        "idempotent_replay": replayed,
    }


@router.post(
    "/threads/{thread_id}/messages/{message_id}/attach",
    status_code=201,
    response_model=ParentPortalMessageAttachmentOut,
)
async def attach_to_message(
    thread_id: int,
    message_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """為自己剛送出的訊息上傳一個附件（一次一檔）。"""
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

    t = _get_thread_for_parent(session, user_id=user_id, thread_id=thread_id)
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
    # 僅自己送出的訊息可掛附件
    if msg.sender_user_id != user_id:
        raise HTTPException(status_code=403, detail="僅可附加在自己的訊息")
    if msg.deleted_at is not None:
        raise HTTPException(status_code=400, detail="已撤回的訊息不可附加附件")

    enforce_student_cross_border(session, t.student_id)
    storage = get_portfolio_storage()
    stored = storage.put_attachment(content, ext)
    att = Attachment(
        owner_type=ATTACHMENT_OWNER_MESSAGE,
        owner_id=msg.id,
        storage_key=stored.storage_key,
        display_key=stored.display_key,
        thumb_key=stored.thumb_key,
        original_filename=safe_attachment_filename(filename, ext),
        mime_type=stored.mime_type,
        size_bytes=len(content),
        uploaded_by=user_id,
    )
    session.add(att)
    session.flush()
    session.refresh(att)
    session.flush()

    request.state.audit_entity_id = str(msg.id)
    request.state.audit_summary = (
        f"家長上傳訊息附件：thread_id={thread_id} message_id={msg.id} "
        f"attachment_id={att.id}"
    )
    return _attachment_to_dict(att)


@router.post("/threads/{thread_id}/read", status_code=200, response_model=OkStatusOut)
def mark_thread_read(
    thread_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    t = _get_thread_for_parent(session, user_id=user_id, thread_id=thread_id)
    mark_read(session, thread=t, role="parent")
    session.flush()
    request.state.audit_skip = True  # 讀已讀；audit 噪音太多
    return {"status": "ok"}


@router.post(
    "/messages/{message_id}/recall", status_code=200, response_model=MessageRecallOut
)
def recall_message(
    message_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    msg = session.query(ParentMessage).filter(ParentMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="訊息不存在")
    # S6: 先驗 thread 參與權，再驗 sender 與時間視窗。
    # Why: 原本只看 can_recall(sender + 30 分鐘)，解綁後家長仍可竄改舊
    # thread 訊息，因 sender_user_id 仍然是該家長 user_id，但 thread
    # 已不屬於該家長。新增 assert_thread_participant 把這條路堵住。
    thread = (
        session.query(ParentMessageThread)
        .filter(ParentMessageThread.id == msg.thread_id)
        .first()
    )
    if not thread:
        raise HTTPException(status_code=404, detail="thread 不存在")
    assert_thread_participant(thread, user_id=user_id, role="parent")
    if not can_recall(msg, user_id=user_id):
        raise HTTPException(status_code=403, detail="只有 sender 30 分鐘內可撤回")
    msg.deleted_at = now_taipei_naive()
    session.flush()
    request.state.audit_entity_id = str(msg.id)
    request.state.audit_summary = (
        f"家長撤回訊息：thread_id={msg.thread_id} message_id={msg.id}"
    )
    return {"status": "ok", "deleted_at": msg.deleted_at.isoformat()}


@router.get("/unread-count", response_model=UnreadCountOut)
def unread_count(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    n = count_unread_for_parent(session, parent_user_id=user_id)
    return {"unread_count": n}
