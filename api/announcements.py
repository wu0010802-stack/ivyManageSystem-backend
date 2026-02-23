"""
Announcements router - Admin CRUD for announcements
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import get_session, Announcement, Employee
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


# ============ Pydantic Models ============

class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = "normal"
    is_pinned: bool = False


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    is_pinned: Optional[bool] = None


# ============ Endpoints ============

@router.get("")
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS)),
):
    """列出所有公告（管理員用）"""
    session = get_session()
    try:
        query = session.query(Announcement).order_by(
            Announcement.is_pinned.desc(),
            Announcement.created_at.desc(),
        )
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()

        results = []
        for ann in items:
            author = session.query(Employee).filter(Employee.id == ann.created_by).first()
            results.append({
                "id": ann.id,
                "title": ann.title,
                "content": ann.content,
                "priority": ann.priority,
                "is_pinned": ann.is_pinned,
                "created_by": ann.created_by,
                "created_by_name": author.name if author else "未知",
                "created_at": ann.created_at.isoformat() if ann.created_at else None,
                "updated_at": ann.updated_at.isoformat() if ann.updated_at else None,
                "read_count": len(ann.reads),
            })

        return {"total": total, "items": results}
    finally:
        session.close()


@router.post("", status_code=201)
def create_announcement(
    data: AnnouncementCreate,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS)),
):
    """新增公告"""
    if data.priority not in ("normal", "important", "urgent"):
        raise HTTPException(status_code=400, detail="無效的優先級")

    session = get_session()
    try:
        ann = Announcement(
            title=data.title,
            content=data.content,
            priority=data.priority,
            is_pinned=data.is_pinned,
            created_by=current_user["employee_id"],
        )
        session.add(ann)
        session.commit()
        return {"message": "公告已發佈", "id": ann.id}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/{announcement_id}")
def update_announcement(
    announcement_id: int,
    data: AnnouncementUpdate,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS)),
):
    """更新公告"""
    session = get_session()
    try:
        ann = session.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not ann:
            raise HTTPException(status_code=404, detail="找不到該公告")

        if data.title is not None:
            ann.title = data.title
        if data.content is not None:
            ann.content = data.content
        if data.priority is not None:
            if data.priority not in ("normal", "important", "urgent"):
                raise HTTPException(status_code=400, detail="無效的優先級")
            ann.priority = data.priority
        if data.is_pinned is not None:
            ann.is_pinned = data.is_pinned

        session.commit()
        return {"message": "公告已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/{announcement_id}")
def delete_announcement(
    announcement_id: int,
    current_user: dict = Depends(require_permission(Permission.ANNOUNCEMENTS)),
):
    """刪除公告"""
    session = get_session()
    try:
        ann = session.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not ann:
            raise HTTPException(status_code=404, detail="找不到該公告")

        session.delete(ann)
        session.commit()
        return {"message": "公告已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
