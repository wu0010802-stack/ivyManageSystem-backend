"""api/parent_portal/contact_book.py — 家長端每日聯絡簿

端點（皆需 require_parent_role + IDOR：學生屬該家長）：
- GET    /api/parent/contact-book/today?student_id=
- GET    /api/parent/contact-book?student_id=&from=&to=&cursor=
- GET    /api/parent/contact-book/{id}
- POST   /api/parent/contact-book/{id}/ack            idempotent
- POST   /api/parent/contact-book/{id}/reply
- DELETE /api/parent/contact-book/{id}/replies/{rid}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from models.database import (
    Attachment,
    Student,
    StudentContactBookAck,
    StudentContactBookEntry,
    StudentContactBookReply,
    get_session,
)
from models.portfolio import ATTACHMENT_OWNER_CONTACT_BOOK
from utils.auth import require_parent_role

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contact-book", tags=["parent-contact-book"])


# ── Pydantic ──────────────────────────────────────────────────────────────


class ReplyCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=500)


# ── Helpers ───────────────────────────────────────────────────────────────


def _load_photos(session, entry_id: int) -> list[Attachment]:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
            Attachment.owner_id == entry_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.created_at.asc())
        .all()
    )


def _entry_to_dict(
    entry: StudentContactBookEntry,
    photos: list[Attachment],
    my_ack_at: Optional[datetime],
) -> dict:
    return {
        "id": entry.id,
        "student_id": entry.student_id,
        "classroom_id": entry.classroom_id,
        "log_date": entry.log_date.isoformat() if entry.log_date else None,
        "mood": entry.mood,
        "meal_lunch": entry.meal_lunch,
        "meal_snack": entry.meal_snack,
        "nap_minutes": entry.nap_minutes,
        "bowel": entry.bowel,
        "temperature_c": (
            float(entry.temperature_c) if entry.temperature_c is not None else None
        ),
        "teacher_note": entry.teacher_note,
        "learning_highlight": entry.learning_highlight,
        "published_at": (
            entry.published_at.isoformat() if entry.published_at else None
        ),
        "my_acknowledged_at": my_ack_at.isoformat() if my_ack_at else None,
        "photos": [
            {
                "id": p.id,
                "display_url": f"/api/parent/uploads/portfolio/{p.display_key or p.storage_key}",
                "thumb_url": (
                    f"/api/parent/uploads/portfolio/{p.thumb_key}"
                    if p.thumb_key
                    else None
                ),
            }
            for p in photos
        ],
    }


def _reply_to_dict(reply: StudentContactBookReply) -> dict:
    return {
        "id": reply.id,
        "entry_id": reply.entry_id,
        "guardian_user_id": reply.guardian_user_id,
        "body": reply.body,
        "created_at": reply.created_at.isoformat() if reply.created_at else None,
    }


def _get_entry_for_parent(
    session, *, user_id: int, entry_id: int
) -> StudentContactBookEntry:
    """取出 entry 並驗證 student 屬此家長 + entry 已發布。"""
    entry = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.id == entry_id,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .first()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="聯絡簿不存在")
    if entry.published_at is None:
        # 草稿對家長一律 404，避免 enumeration
        raise HTTPException(status_code=404, detail="聯絡簿不存在")
    _assert_student_owned(session, user_id, entry.student_id)
    return entry


def _get_my_ack_at(session, entry_id: int, user_id: int) -> Optional[datetime]:
    row = (
        session.query(StudentContactBookAck)
        .filter(
            StudentContactBookAck.entry_id == entry_id,
            StudentContactBookAck.guardian_user_id == user_id,
        )
        .first()
    )
    return row.read_at if row else None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/today")
def get_today(
    student_id: int = Query(..., gt=0),
    current_user: dict = Depends(require_parent_role()),
):
    """取得指定子女今日已發布的聯絡簿（沒有 entry / 仍為草稿時 entry 回 null）。"""
    user_id = current_user["user_id"]
    today = date.today()
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        entry = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.student_id == student_id,
                StudentContactBookEntry.log_date == today,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
            )
            .first()
        )
        if not entry:
            return {
                "student_id": student_id,
                "log_date": today.isoformat(),
                "entry": None,
            }
        photos = _load_photos(session, entry.id)
        my_ack_at = _get_my_ack_at(session, entry.id, user_id)
        return {
            "student_id": student_id,
            "log_date": today.isoformat(),
            "entry": _entry_to_dict(entry, photos, my_ack_at),
        }
    finally:
        session.close()


@router.get("")
def list_history(
    student_id: int = Query(..., gt=0),
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    limit: int = Query(default=30, ge=1, le=100),
    current_user: dict = Depends(require_parent_role()),
):
    """歷史清單（僅 published）。預設回最近 30 筆。"""
    user_id = current_user["user_id"]
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=60)
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="日期區間不正確")

    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        rows = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.student_id == student_id,
                StudentContactBookEntry.log_date >= from_date,
                StudentContactBookEntry.log_date <= to_date,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
            )
            .order_by(StudentContactBookEntry.log_date.desc())
            .limit(limit)
            .all()
        )
        # 一次撈所有 ack 與 photo，避免 N+1
        entry_ids = [e.id for e in rows]
        ack_map: dict[int, datetime] = {}
        if entry_ids:
            for r in (
                session.query(StudentContactBookAck)
                .filter(
                    StudentContactBookAck.entry_id.in_(entry_ids),
                    StudentContactBookAck.guardian_user_id == user_id,
                )
                .all()
            ):
                ack_map[r.entry_id] = r.read_at

        photo_map: dict[int, list[Attachment]] = {eid: [] for eid in entry_ids}
        if entry_ids:
            for a in (
                session.query(Attachment)
                .filter(
                    Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
                    Attachment.owner_id.in_(entry_ids),
                    Attachment.deleted_at.is_(None),
                )
                .order_by(Attachment.created_at.asc())
                .all()
            ):
                photo_map.setdefault(a.owner_id, []).append(a)

        return {
            "student_id": student_id,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "entries": [
                _entry_to_dict(e, photo_map.get(e.id, []), ack_map.get(e.id))
                for e in rows
            ],
        }
    finally:
        session.close()


@router.get("/{entry_id}")
def get_detail(
    entry_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """單筆詳情（含 reply 列表）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        entry = _get_entry_for_parent(session, user_id=user_id, entry_id=entry_id)
        photos = _load_photos(session, entry.id)
        my_ack_at = _get_my_ack_at(session, entry.id, user_id)
        replies = (
            session.query(StudentContactBookReply)
            .filter(
                StudentContactBookReply.entry_id == entry.id,
                StudentContactBookReply.deleted_at.is_(None),
            )
            .order_by(StudentContactBookReply.created_at.asc())
            .all()
        )
        return {
            **_entry_to_dict(entry, photos, my_ack_at),
            "replies": [_reply_to_dict(r) for r in replies],
        }
    finally:
        session.close()


@router.post("/{entry_id}/ack")
def mark_read(
    entry_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """家長已讀；idempotent（已存在的 ack row 直接回 200）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        entry = _get_entry_for_parent(session, user_id=user_id, entry_id=entry_id)

        existing = (
            session.query(StudentContactBookAck)
            .filter(
                StudentContactBookAck.entry_id == entry.id,
                StudentContactBookAck.guardian_user_id == user_id,
            )
            .first()
        )
        if existing:
            return {
                "entry_id": entry.id,
                "read_at": existing.read_at.isoformat() if existing.read_at else None,
                "already_marked": True,
            }
        ack = StudentContactBookAck(
            entry_id=entry.id,
            guardian_user_id=user_id,
            read_at=datetime.now(),
        )
        session.add(ack)
        session.commit()

        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"家長已讀聯絡簿：entry={entry.id} student={entry.student_id} "
            f"parent_user={user_id}"
        )

        # WS 推回班級教師端，讓老師看到 ack 計數即時更新
        try:
            from api.contact_book_ws import broadcast_classroom

            import asyncio

            async def _push():
                await broadcast_classroom(
                    entry.classroom_id,
                    {
                        "type": "contact_book_acked",
                        "entry_id": entry.id,
                        "student_id": entry.student_id,
                        "classroom_id": entry.classroom_id,
                        "read_at": ack.read_at.isoformat(),
                    },
                )

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_push())
                else:
                    loop.run_until_complete(_push())
            except RuntimeError:
                asyncio.run(_push())
        except Exception as exc:
            logger.warning("contact_book ack WS 推送失敗（不阻斷）：%s", exc)

        return {
            "entry_id": entry.id,
            "read_at": ack.read_at.isoformat(),
            "already_marked": False,
        }
    finally:
        session.close()


@router.post("/{entry_id}/reply", status_code=201)
def reply(
    entry_id: int,
    payload: ReplyCreate,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """家長簡短回覆。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        entry = _get_entry_for_parent(session, user_id=user_id, entry_id=entry_id)
        row = StudentContactBookReply(
            entry_id=entry.id,
            guardian_user_id=user_id,
            body=payload.body.strip(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)

        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"家長回覆聯絡簿：entry={entry.id} reply={row.id} " f"parent_user={user_id}"
        )
        return _reply_to_dict(row)
    finally:
        session.close()


@router.delete("/{entry_id}/replies/{reply_id}")
def delete_reply(
    entry_id: int,
    reply_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """家長軟刪自己的回覆。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        entry = _get_entry_for_parent(session, user_id=user_id, entry_id=entry_id)
        row = (
            session.query(StudentContactBookReply)
            .filter(
                StudentContactBookReply.id == reply_id,
                StudentContactBookReply.entry_id == entry.id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="回覆不存在")
        if row.guardian_user_id != user_id:
            raise HTTPException(status_code=403, detail="不可刪除他人回覆")
        if row.deleted_at:
            return {"message": "回覆已刪除"}
        row.deleted_at = datetime.now()
        session.commit()
        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"家長刪除聯絡簿回覆：entry={entry.id} reply={reply_id}"
        )
        return {"message": "刪除成功"}
    finally:
        session.close()
