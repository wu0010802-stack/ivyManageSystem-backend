"""
Announcements router - Admin CRUD for announcements
"""

import logging
from html.parser import HTMLParser
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel
from sqlalchemy.orm import joinedload, selectinload

from models.database import get_session, Announcement, AnnouncementRecipient, Employee
from utils.auth import require_permission
from utils.error_messages import ANNOUNCEMENT_NOT_FOUND
from utils.permissions import Permission


class _TagStripper(HTMLParser):
    """HTMLParser subclass that discards all tags and keeps only text nodes.

    `convert_charrefs=False` is intentional: it prevents entity-encoded
    payloads (e.g. ``&lt;img onerror=…&gt;``) from being decoded into real
    ``<`` / ``>`` characters before tag-stripping, which would let them bypass
    the filter and be stored as raw HTML in the database.
    Named entities and character references are re-emitted verbatim so that
    legitimate content like ``&amp;`` or ``&copy;`` is preserved.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        # Preserve named HTML entities (e.g. &lt; stays as &lt;)
        self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        # Preserve numeric character references (e.g. &#60; stays as &#60;)
        self._chunks.append(f"&#{name};")

    def get_text(self) -> str:
        return "".join(self._chunks)


def _strip_html(text: str) -> str:
    """Strip all HTML tags from *text*, returning plain-text content only.

    HTML entities such as ``&lt;`` are intentionally preserved (not decoded)
    so they remain safe regardless of how the stored value is later rendered.
    """
    if not text:
        return text
    p = _TagStripper()
    p.feed(text)
    p.close()
    return p.get_text()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


# ============ Pydantic Models ============

class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = "normal"
    is_pinned: bool = False
    target_employee_ids: Optional[List[int]] = None  # None / [] = 全員可見


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    is_pinned: Optional[bool] = None
    target_employee_ids: Optional[List[int]] = None  # None = 不變；[] = 改為全員可見


# ============ Endpoints ============

@router.get("")
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS_READ)),
):
    """列出所有公告（管理員用）"""
    session = get_session()
    try:
        query = session.query(Announcement).options(
            joinedload(Announcement.author),
            selectinload(Announcement.reads),
            selectinload(Announcement.recipients),
        ).order_by(
            Announcement.is_pinned.desc(),
            Announcement.created_at.desc(),
        )
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()

        read_employee_ids = {
            read.employee_id
            for ann in items
            for read in ann.reads
            if read.employee_id is not None
        }
        read_employee_map = {}
        if read_employee_ids:
            employees = session.query(Employee.id, Employee.name).filter(Employee.id.in_(read_employee_ids)).all()
            read_employee_map = {employee.id: employee.name for employee in employees}

        results = []
        for ann in items:
            recipient_ids = [r.employee_id for r in ann.recipients]
            sorted_reads = sorted(
                ann.reads,
                key=lambda read: read.read_at or 0,
                reverse=True,
            )
            readers = [
                {
                    "employee_id": read.employee_id,
                    "name": read_employee_map.get(read.employee_id, "未知"),
                    "read_at": read.read_at.isoformat() if read.read_at else None,
                }
                for read in sorted_reads
            ]
            results.append({
                "id": ann.id,
                "title": ann.title,
                "content": ann.content,
                "priority": ann.priority,
                "is_pinned": ann.is_pinned,
                "created_by": ann.created_by,
                "created_by_name": ann.author.name if ann.author else "未知",
                "created_at": ann.created_at.isoformat() if ann.created_at else None,
                "updated_at": ann.updated_at.isoformat() if ann.updated_at else None,
                "read_count": len(ann.reads),
                "read_preview": readers[:3],
                "has_more_readers": len(readers) > 3,
                "readers": readers,
                "recipient_count": len(recipient_ids),
                "recipient_ids": recipient_ids,
            })

        return {"total": total, "items": results}
    finally:
        session.close()


@router.post("", status_code=201)
def create_announcement(
    data: AnnouncementCreate,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS_WRITE)),
):
    """新增公告"""
    if data.priority not in ("normal", "important", "urgent"):
        raise HTTPException(status_code=400, detail="無效的優先級")

    session = get_session()
    try:
        ann = Announcement(
            title=_strip_html(data.title),
            content=_strip_html(data.content),
            priority=data.priority,
            is_pinned=data.is_pinned,
            created_by=current_user["employee_id"],
        )
        session.add(ann)
        session.flush()  # 取得 ann.id

        if data.target_employee_ids:
            for emp_id in data.target_employee_ids:
                session.add(AnnouncementRecipient(announcement_id=ann.id, employee_id=emp_id))

        session.commit()
        return {"message": "公告已發佈", "id": ann.id}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/{announcement_id}")
def update_announcement(
    announcement_id: int,
    data: AnnouncementUpdate,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS_WRITE)),
):
    """更新公告"""
    session = get_session()
    try:
        ann = session.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)

        if data.title is not None:
            ann.title = _strip_html(data.title)
        if data.content is not None:
            ann.content = _strip_html(data.content)
        if data.priority is not None:
            if data.priority not in ("normal", "important", "urgent"):
                raise HTTPException(status_code=400, detail="無效的優先級")
            ann.priority = data.priority
        if data.is_pinned is not None:
            ann.is_pinned = data.is_pinned

        if data.target_employee_ids is not None:
            # 清除舊 recipients，再批次 INSERT 新的
            session.query(AnnouncementRecipient).filter(
                AnnouncementRecipient.announcement_id == announcement_id
            ).delete()
            for emp_id in data.target_employee_ids:
                session.add(AnnouncementRecipient(announcement_id=announcement_id, employee_id=emp_id))

        session.commit()
        return {"message": "公告已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/{announcement_id}")
def delete_announcement(
    announcement_id: int,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS_WRITE)),
):
    """刪除公告"""
    session = get_session()
    try:
        ann = session.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)

        session.delete(ann)
        session.commit()
        return {"message": "公告已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
