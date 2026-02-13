"""
Portal - announcement endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, Employee, Announcement, AnnouncementRead
from utils.auth import get_current_user

router = APIRouter()


@router.get("/announcements")
def get_portal_announcements(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """取得公告列表（教師端，分頁）"""
    session = get_session()
    try:
        emp_id = current_user["employee_id"]

        base_q = session.query(Announcement, Employee.name).outerjoin(
            Employee, Announcement.created_by == Employee.id
        ).order_by(
            Announcement.is_pinned.desc(),
            Announcement.created_at.desc(),
        )

        total = session.query(Announcement).count()
        rows = base_q.offset(skip).limit(limit).all()

        ann_ids = [ann.id for ann, _ in rows]
        read_ids = set()
        if ann_ids:
            read_ids = set(
                r.announcement_id for r in session.query(AnnouncementRead).filter(
                    AnnouncementRead.employee_id == emp_id,
                    AnnouncementRead.announcement_id.in_(ann_ids),
                ).all()
            )

        items = []
        for ann, author_name in rows:
            items.append({
                "id": ann.id,
                "title": ann.title,
                "content": ann.content,
                "priority": ann.priority,
                "is_pinned": ann.is_pinned,
                "created_by_name": author_name or "未知",
                "created_at": ann.created_at.isoformat() if ann.created_at else None,
                "is_read": ann.id in read_ids,
            })

        return {"items": items, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


@router.post("/announcements/{announcement_id}/read")
def mark_announcement_read(
    announcement_id: int,
    current_user: dict = Depends(get_current_user),
):
    """標記公告為已讀"""
    session = get_session()
    try:
        emp_id = current_user["employee_id"]

        ann = session.query(Announcement).filter(Announcement.id == announcement_id).first()
        if not ann:
            raise HTTPException(status_code=404, detail="找不到該公告")

        existing = session.query(AnnouncementRead).filter(
            AnnouncementRead.announcement_id == announcement_id,
            AnnouncementRead.employee_id == emp_id,
        ).first()

        if not existing:
            read_record = AnnouncementRead(
                announcement_id=announcement_id,
                employee_id=emp_id,
            )
            session.add(read_record)
            session.commit()

        return {"message": "已標記為已讀"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/unread-count")
def get_unread_count(
    current_user: dict = Depends(get_current_user),
):
    """取得未讀公告數量"""
    session = get_session()
    try:
        emp_id = current_user["employee_id"]

        total = session.query(Announcement).count()
        read = session.query(AnnouncementRead).filter(
            AnnouncementRead.employee_id == emp_id,
        ).count()

        return {"unread_count": max(0, total - read)}
    finally:
        session.close()
