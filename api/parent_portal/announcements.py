"""api/parent_portal/announcements.py — 家長端公告。

可見性規則（A.5）：
- scope='all' → 所有家長
- scope='classroom' → 監護學生所屬班級
- scope='student' → 監護學生
- scope='guardian' → 該監護人本人

未讀計數 = 可見公告 minus AnnouncementParentRead。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, or_, select

from models.database import (
    Announcement,
    AnnouncementParentRead,
    AnnouncementParentRecipient,
    Classroom,
    Student,
    get_session,
)
from utils.auth import require_parent_role

from ._shared import _get_parent_student_ids

router = APIRouter(prefix="/announcements", tags=["parent-announcements"])


def _build_visibility_subquery(session, user_id: int):
    """回傳：家長可見公告 id 的 subquery（用 EXISTS）。"""
    guardian_ids, student_ids = _get_parent_student_ids(session, user_id)
    classroom_ids = []
    if student_ids:
        rows = (
            session.query(Student.classroom_id)
            .filter(Student.id.in_(student_ids), Student.classroom_id.isnot(None))
            .distinct()
            .all()
        )
        classroom_ids = [r[0] for r in rows]

    apr = AnnouncementParentRecipient
    conditions = [apr.scope == "all"]
    if classroom_ids:
        conditions.append(
            and_(apr.scope == "classroom", apr.classroom_id.in_(classroom_ids))
        )
    if student_ids:
        conditions.append(and_(apr.scope == "student", apr.student_id.in_(student_ids)))
    if guardian_ids:
        conditions.append(
            and_(apr.scope == "guardian", apr.guardian_id.in_(guardian_ids))
        )
    return or_(*conditions)


@router.get("")
def list_announcements(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        cond = _build_visibility_subquery(session, user_id)
        apr = AnnouncementParentRecipient
        visible_subq = exists().where(
            and_(apr.announcement_id == Announcement.id, cond)
        )
        q = (
            session.query(Announcement)
            .filter(visible_subq)
            .order_by(Announcement.created_at.desc())
        )
        total = q.count()
        rows = q.offset(skip).limit(limit).all()

        # 一次撈該家長的已讀公告 id（只算回傳的這批）
        ann_ids = [r.id for r in rows]
        read_ids = set()
        if ann_ids:
            reads = (
                session.query(AnnouncementParentRead.announcement_id)
                .filter(
                    AnnouncementParentRead.user_id == user_id,
                    AnnouncementParentRead.announcement_id.in_(ann_ids),
                )
                .all()
            )
            read_ids = {r[0] for r in reads}

        items = [
            {
                "id": a.id,
                "title": a.title,
                "content": a.content,
                "priority": a.priority,
                "is_pinned": bool(a.is_pinned),
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "is_read": a.id in read_ids,
            }
            for a in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


def count_unread_for_user(session, user_id: int) -> int:
    """家長未讀公告數（可見 minus AnnouncementParentRead）。

    可被 home/summary 等彙總端點重用，避免再起一支 RTT。
    """
    cond = _build_visibility_subquery(session, user_id)
    apr = AnnouncementParentRecipient
    visible_subq = exists().where(and_(apr.announcement_id == Announcement.id, cond))
    read_ids_select = select(AnnouncementParentRead.announcement_id).where(
        AnnouncementParentRead.user_id == user_id
    )
    return (
        session.query(Announcement)
        .filter(visible_subq, ~Announcement.id.in_(read_ids_select))
        .count()
    )


@router.get("/unread-count")
def unread_count(current_user: dict = Depends(require_parent_role())):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        return {"unread_count": count_unread_for_user(session, user_id)}
    finally:
        session.close()


@router.post("/{announcement_id}/read", status_code=200)
def mark_read(
    announcement_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """冪等標記為已讀。若家長對此公告無可見權，回 403。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # 先確認可見性，避免「已讀」洩漏不可見公告的存在
        cond = _build_visibility_subquery(session, user_id)
        apr = AnnouncementParentRecipient
        visible = (
            session.query(Announcement)
            .filter(
                Announcement.id == announcement_id,
                exists().where(and_(apr.announcement_id == Announcement.id, cond)),
            )
            .first()
        )
        if visible is None:
            raise HTTPException(status_code=403, detail="此公告不在可見範圍")

        existing = (
            session.query(AnnouncementParentRead)
            .filter(
                AnnouncementParentRead.announcement_id == announcement_id,
                AnnouncementParentRead.user_id == user_id,
            )
            .first()
        )
        if existing is None:
            session.add(
                AnnouncementParentRead(
                    announcement_id=announcement_id,
                    user_id=user_id,
                    read_at=datetime.now(),
                )
            )
            session.commit()
        return {"status": "ok"}
    finally:
        session.close()
