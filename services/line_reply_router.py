"""services/line_reply_router.py — LINE webhook 家長端訊息路由（Phase 5）

家長從 LINE 對話框打字 → webhook → 寫入對應 thread 的 ParentMessage(source='line')。

設計：postback-driven 主動式
- 教師發訊推播時帶 quick-reply postback `thread_id={N}`
- 家長點 → webhook 收到 postback → upsert LineReplyContext (10 min TTL)
- 後續 10 分鐘內收到的純文字訊息 → 寫到該 thread
- 無 context（過期或從未選）→ 列家長未讀 thread (max 5) 組 quick-reply

webhookEventId UNIQUE 防 LINE retry 造成重複。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError

from models.database import (
    LineReplyContext,
    LineWebhookEvent,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
)
from services.parent_message_service import append_message

logger = logging.getLogger(__name__)

# postback context TTL：10 分鐘
CONTEXT_TTL = timedelta(minutes=10)
# quick-reply 最多 5 個按鈕（LINE 限制）
QUICK_REPLY_MAX = 5


def deduplicate_event(
    session, *, webhook_event_id: str, event_type: str, line_user_id: Optional[str]
) -> bool:
    """處理重複 webhook event：已見過回 False（caller 應跳過）；首次回 True 並 insert row。"""
    if not webhook_event_id:
        return True
    row = LineWebhookEvent(
        webhook_event_id=webhook_event_id,
        event_type=event_type,
        line_user_id=line_user_id,
        processed_at=datetime.now(),
    )
    session.add(row)
    try:
        session.flush()
        return True
    except IntegrityError:
        session.rollback()
        return False


def _active_context(session, line_user_id: str) -> Optional[LineReplyContext]:
    ctx = (
        session.query(LineReplyContext)
        .filter(LineReplyContext.line_user_id == line_user_id)
        .first()
    )
    if not ctx:
        return None
    if ctx.expires_at and ctx.expires_at < datetime.now():
        return None
    return ctx


def upsert_reply_context(
    session, *, line_user_id: str, thread_id: int
) -> LineReplyContext:
    """postback 後 / reply 後刷新 context；UNIQUE(line_user_id) 確保只一筆。"""
    ctx = (
        session.query(LineReplyContext)
        .filter(LineReplyContext.line_user_id == line_user_id)
        .first()
    )
    expires = datetime.now() + CONTEXT_TTL
    if ctx:
        ctx.thread_id = thread_id
        ctx.expires_at = expires
    else:
        ctx = LineReplyContext(
            line_user_id=line_user_id, thread_id=thread_id, expires_at=expires
        )
        session.add(ctx)
        session.flush()
    return ctx


def _list_unread_threads_for_parent(
    session, parent_user_id: int
) -> list[tuple[ParentMessageThread, str, str]]:
    """回傳 [(thread, student_name, teacher_name), ...]，最多 QUICK_REPLY_MAX 筆，
    優先未讀，未讀內依 last_message_at desc。"""
    threads = (
        session.query(ParentMessageThread)
        .filter(
            ParentMessageThread.parent_user_id == parent_user_id,
            ParentMessageThread.deleted_at.is_(None),
        )
        .order_by(
            ParentMessageThread.last_message_at.is_(None).asc(),
            ParentMessageThread.last_message_at.desc(),
        )
        .limit(QUICK_REPLY_MAX)
        .all()
    )
    out = []
    for t in threads:
        student = session.query(Student).filter(Student.id == t.student_id).first()
        teacher = session.query(User).filter(User.id == t.teacher_user_id).first()
        out.append(
            (
                t,
                student.name if student else "孩子",
                teacher.username if teacher else "老師",
            )
        )
    return out


def build_thread_quick_reply(
    threads: list[tuple[ParentMessageThread, str, str]],
) -> dict:
    """組 LINE quickReply payload；給 _line_service 直接放進 messages 結構。"""
    items = []
    for t, sname, tname in threads:
        label = f"{sname} / {tname}"
        if len(label) > 20:
            label = label[:18] + "…"
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"thread_id={t.id}",
                    "displayText": f"回覆給 {tname}（{sname}）",
                },
            }
        )
    return {"items": items}


def handle_parent_text_message(
    session,
    *,
    line_service,
    parent_user: User,
    text: str,
    reply_token: str,
) -> None:
    """家長 LINE 對話打字進來時。"""
    if not text or not text.strip():
        return
    ctx = _active_context(session, parent_user.line_user_id)
    if ctx is None:
        # 列出可回覆 thread；無則提示去 LIFF
        threads = _list_unread_threads_for_parent(session, parent_user.id)
        if not threads:
            line_service._reply(
                reply_token,
                "目前沒有可回覆的訊息。請開啟家長 App 查看。",
            )
            return
        # 用 messages payload with quickReply
        body = "請選擇要回覆的訊息："
        line_service._reply_with_quick_reply(
            reply_token, body, build_thread_quick_reply(threads)
        )
        return

    # 有 context：寫到該 thread
    thread = (
        session.query(ParentMessageThread)
        .filter(ParentMessageThread.id == ctx.thread_id)
        .first()
    )
    if not thread or thread.parent_user_id != parent_user.id:
        line_service._reply(reply_token, "目前的對話已過期，請開啟家長 App 重新選擇。")
        return

    msg, _ = append_message(
        session,
        thread=thread,
        sender_user_id=parent_user.id,
        sender_role="parent",
        body=text.strip(),
        client_request_id=None,
        source="line",
    )
    # 刷新 context expires_at
    ctx.expires_at = datetime.now() + CONTEXT_TTL
    session.commit()
    line_service._reply(reply_token, "✅ 已送出")
    logger.info(
        "LINE 家長回訊：parent_user=%d thread_id=%d msg_id=%d",
        parent_user.id,
        thread.id,
        msg.id,
    )


def handle_parent_postback(
    session,
    *,
    line_service,
    parent_user: User,
    data: str,
    reply_token: str,
) -> None:
    """LINE postback：data='thread_id=N' → upsert context。"""
    if not data or not data.startswith("thread_id="):
        return
    try:
        thread_id = int(data.split("=", 1)[1])
    except (ValueError, IndexError):
        return
    thread = (
        session.query(ParentMessageThread)
        .filter(ParentMessageThread.id == thread_id)
        .first()
    )
    if not thread or thread.parent_user_id != parent_user.id:
        line_service._reply(reply_token, "找不到對應的對話，請開啟家長 App。")
        return
    upsert_reply_context(
        session, line_user_id=parent_user.line_user_id, thread_id=thread.id
    )
    session.commit()

    student = session.query(Student).filter(Student.id == thread.student_id).first()
    teacher = session.query(User).filter(User.id == thread.teacher_user_id).first()
    line_service._reply(
        reply_token,
        f"請輸入要回覆給 {teacher.username if teacher else '老師'}"
        f"（{student.name if student else '孩子'}）的訊息：",
    )
