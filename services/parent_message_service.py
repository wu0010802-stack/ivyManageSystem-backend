"""services/parent_message_service.py — 家園溝通平台共用邏輯

家長端與員工端 router 共享：thread upsert、班導師守衛、IDOR 守衛、撤回視窗。

設計：純函式集合，呼叫端應已開啟 transaction。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from models.classroom import Classroom, Student
from models.parent_message import ParentMessage, ParentMessageThread

# sender 撤回視窗：30 分鐘
RECALL_WINDOW = timedelta(minutes=30)


def assert_teacher_is_homeroom(
    session,
    *,
    employee_id: int,
    student_id: int,
) -> Classroom:
    """守衛：current employee 必須是該 student 所屬 classroom 的 head_teacher。

    admin role 與 supervisor role 雖不受 portfolio 班級限制，但本案決策（業主）
    要求 thread 發起嚴格限「班導師」。admin role bypass 由 endpoint 層處理
    （current_user["role"] == "admin"），此 helper 不做角色判定。

    成功回傳 Classroom；不符 → 403。
    """
    student = session.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="學生不存在")
    if not student.classroom_id:
        raise HTTPException(status_code=403, detail="此學生未分班，無法發起家長訊息")
    classroom = (
        session.query(Classroom).filter(Classroom.id == student.classroom_id).first()
    )
    if not classroom or classroom.head_teacher_id != employee_id:
        raise HTTPException(
            status_code=403,
            detail="僅該班導師可發起家長訊息（助教 / 才藝可回覆既有 thread）",
        )
    return classroom


def get_or_create_thread(
    session,
    *,
    parent_user_id: int,
    teacher_user_id: int,
    student_id: int,
) -> ParentMessageThread:
    """三元組唯一 thread；不存在則建立。"""
    thread = (
        session.query(ParentMessageThread)
        .filter(
            ParentMessageThread.parent_user_id == parent_user_id,
            ParentMessageThread.teacher_user_id == teacher_user_id,
            ParentMessageThread.student_id == student_id,
            ParentMessageThread.deleted_at.is_(None),
        )
        .first()
    )
    if thread:
        return thread
    thread = ParentMessageThread(
        parent_user_id=parent_user_id,
        teacher_user_id=teacher_user_id,
        student_id=student_id,
    )
    session.add(thread)
    try:
        session.flush()
    except IntegrityError:
        # race：另一個 request 同時建立 → rollback flush 後重查
        session.rollback()
        thread = (
            session.query(ParentMessageThread)
            .filter(
                ParentMessageThread.parent_user_id == parent_user_id,
                ParentMessageThread.teacher_user_id == teacher_user_id,
                ParentMessageThread.student_id == student_id,
            )
            .first()
        )
    return thread


def assert_thread_participant(
    thread: ParentMessageThread, *, user_id: int, role: str
) -> None:
    """IDOR 守衛：caller 必須是 thread 的 parent 或 teacher。"""
    if role == "parent":
        if thread.parent_user_id != user_id:
            raise HTTPException(status_code=403, detail="此 thread 不屬於您")
    elif role == "teacher":
        if thread.teacher_user_id != user_id:
            raise HTTPException(status_code=403, detail="此 thread 不屬於您")
    else:
        raise HTTPException(status_code=403, detail="不支援的角色")


def append_message(
    session,
    *,
    thread: ParentMessageThread,
    sender_user_id: int,
    sender_role: str,
    body: str | None,
    client_request_id: str | None,
    source: str = "app",
) -> tuple[ParentMessage, bool]:
    """寫入訊息；回 (message, is_replay)。

    若帶 client_request_id 且該 (thread_id, client_request_id) 已存在，
    則直接回放原 message（is_replay=True）。
    """
    if client_request_id:
        existing = (
            session.query(ParentMessage)
            .filter(
                ParentMessage.thread_id == thread.id,
                ParentMessage.client_request_id == client_request_id,
            )
            .first()
        )
        if existing:
            return existing, True

    msg = ParentMessage(
        thread_id=thread.id,
        sender_user_id=sender_user_id,
        sender_role=sender_role,
        body=body,
        client_request_id=client_request_id,
        source=source,
    )
    session.add(msg)
    try:
        session.flush()
    except IntegrityError:
        # race on (thread_id, client_request_id) → 撈回 replay
        session.rollback()
        replay = (
            session.query(ParentMessage)
            .filter(
                ParentMessage.thread_id == thread.id,
                ParentMessage.client_request_id == client_request_id,
            )
            .first()
        )
        if replay:
            return replay, True
        raise

    # 更新 thread.last_message_at
    thread.last_message_at = msg.created_at or datetime.now()
    return msg, False


def can_recall(msg: ParentMessage, *, user_id: int) -> bool:
    """sender 自己且 30 分鐘內可撤回。"""
    if msg.sender_user_id != user_id:
        return False
    if msg.deleted_at is not None:
        return False
    created = msg.created_at or datetime.now()
    return datetime.now() - created <= RECALL_WINDOW


def mark_read(
    session, *, thread: ParentMessageThread, role: str, when: datetime | None = None
) -> None:
    """更新 thread.parent_last_read_at / teacher_last_read_at。"""
    when = when or datetime.now()
    if role == "parent":
        thread.parent_last_read_at = when
    elif role == "teacher":
        thread.teacher_last_read_at = when


def count_unread_for_parent(session, *, parent_user_id: int) -> int:
    """家長未讀訊息數：所有 thread 中 sender_role='teacher' 且 created_at > parent_last_read_at。"""
    threads = (
        session.query(ParentMessageThread)
        .filter(
            ParentMessageThread.parent_user_id == parent_user_id,
            ParentMessageThread.deleted_at.is_(None),
        )
        .all()
    )
    total = 0
    for t in threads:
        cutoff = t.parent_last_read_at
        q = session.query(ParentMessage).filter(
            ParentMessage.thread_id == t.id,
            ParentMessage.sender_role == "teacher",
            ParentMessage.deleted_at.is_(None),
        )
        if cutoff is not None:
            q = q.filter(ParentMessage.created_at > cutoff)
        total += q.count()
    return total
