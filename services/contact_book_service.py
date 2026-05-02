"""services/contact_book_service.py — 聯絡簿發布／統計集中入口

publish_entry：唯一發布路徑
- 標 published_at + 累加 version
- WS 廣播給該班級教師端 + 每位 guardian 的家長端
- LINE push（透過 should_push_to_parent gate）給每位有綁 LINE 的 guardian

compute_class_completion：教師後台用「今日 X/Y 已發布」進度條
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    Attachment,
    Classroom,
    Guardian,
    Student,
    StudentContactBookEntry,
    User,
)
from models.portfolio import ATTACHMENT_OWNER_CONTACT_BOOK
from services.line_service import LineService

logger = logging.getLogger(__name__)


def _count_photos(session: Session, entry_id: int) -> int:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
            Attachment.owner_id == entry_id,
            Attachment.deleted_at.is_(None),
        )
        .count()
    )


def _gather_guardian_user_ids(session: Session, student_id: int) -> list[int]:
    rows = (
        session.query(Guardian.user_id)
        .filter(Guardian.student_id == student_id, Guardian.deleted_at.is_(None))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def publish_entry(
    session: Session,
    *,
    entry_id: int,
    line_service: Optional[LineService] = None,
) -> StudentContactBookEntry:
    """發布一筆聯絡簿 entry。

    呼叫前 caller 已完成權限檢查；本函式只負責：
    1. 標 published_at + 累加 version
    2. 推送 WS（教師班級 + 家長個人）
    3. 推送 LINE（透過 should_push_to_parent gate）

    回傳更新後的 entry（caller 仍持 session 控制 commit）。
    """
    entry = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.id == entry_id,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .first()
    )
    if entry is None:
        raise ValueError(f"contact_book entry not found: {entry_id}")

    if entry.published_at is None:
        entry.published_at = datetime.now()
    entry.version = (entry.version or 1) + 1
    session.flush()

    student = session.query(Student).filter(Student.id == entry.student_id).first()
    classroom = (
        session.query(Classroom).filter(Classroom.id == entry.classroom_id).first()
    )
    student_name = student.name if student else f"student#{entry.student_id}"
    photo_count = _count_photos(session, entry.id)

    guardian_user_ids = _gather_guardian_user_ids(session, entry.student_id)

    # 廣播 payload — 同 event 內含足夠 metadata 讓前端決定刷新範圍
    event_payload = {
        "type": "contact_book_published",
        "entry_id": entry.id,
        "student_id": entry.student_id,
        "classroom_id": entry.classroom_id,
        "log_date": entry.log_date.isoformat() if entry.log_date else None,
        "published_at": (
            entry.published_at.isoformat() if entry.published_at else None
        ),
    }

    # WS broadcasting：fire-and-forget；單筆失敗不影響交易
    try:
        from api.contact_book_ws import broadcast_classroom, broadcast_parent

        async def _fanout():
            await broadcast_classroom(entry.classroom_id, event_payload)
            for uid in guardian_user_ids:
                await broadcast_parent(uid, event_payload)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_fanout())
            else:
                loop.run_until_complete(_fanout())
        except RuntimeError:
            asyncio.run(_fanout())
    except Exception as exc:
        logger.warning("contact_book WS 廣播失敗（不阻斷）：%s", exc)

    # LINE push：依個別家長偏好決定
    if line_service is not None:
        teacher_note_preview = (entry.teacher_note or "").strip()
        for uid in guardian_user_ids:
            line_id = line_service.should_push_to_parent(
                session, user_id=uid, event_type="contact_book_published"
            )
            if not line_id:
                continue
            try:
                line_service.notify_parent_contact_book_published(
                    line_id,
                    student_name=student_name,
                    log_date=entry.log_date,
                    teacher_note_preview=teacher_note_preview,
                    photo_count=photo_count,
                )
            except Exception as exc:
                logger.warning("contact_book LINE push 失敗 user_id=%d: %s", uid, exc)

    return entry


def compute_class_completion(
    session: Session,
    *,
    classroom_id: int,
    log_date,
) -> dict:
    """回傳該班該日聯絡簿完成度：roster 數 / 草稿數 / 已發布數。"""
    roster = (
        session.query(Student)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active.is_(True),
        )
        .count()
    )
    entries = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.classroom_id == classroom_id,
            StudentContactBookEntry.log_date == log_date,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .all()
    )
    draft = sum(1 for e in entries if e.published_at is None)
    published = sum(1 for e in entries if e.published_at is not None)
    return {
        "roster": roster,
        "draft": draft,
        "published": published,
        "missing": max(roster - draft - published, 0),
    }
