"""
Portal - announcement endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from utils.errors import raise_safe_500

from models.database import (
    get_session,
    Employee,
    Announcement,
    AnnouncementRead,
    AnnouncementRecipient,
)
from utils.auth import get_current_user
from utils.error_messages import ANNOUNCEMENT_NOT_FOUND

from ._shared import check_etag, compute_etag

router = APIRouter()


@router.get("/announcements")
def get_portal_announcements(
    request: Request,
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """取得公告列表（教師端，分頁）"""
    session = get_session()
    try:
        emp_id = current_user["employee_id"]

        # Phase 8 ETag：用 max(updated_at, created_at) + employee_id 算 weak ETag
        max_changed = session.query(
            func.max(Announcement.updated_at),
            func.max(Announcement.created_at),
        ).first()
        max_ts = max(filter(None, max_changed or []), default=None)
        etag_payload = f"emp={emp_id}|skip={skip}|limit={limit}|ts={max_ts.isoformat() if max_ts else 'none'}"
        etag = compute_etag(etag_payload)
        cached = check_etag(request, etag)
        if cached:
            return cached

        # 過濾：全員公告（無 recipients）或指定包含當前員工的公告
        no_recipients_subq = (
            ~session.query(AnnouncementRecipient)
            .filter(AnnouncementRecipient.announcement_id == Announcement.id)
            .exists()
        )
        targeted_to_me_subq = (
            session.query(AnnouncementRecipient)
            .filter(
                AnnouncementRecipient.announcement_id == Announcement.id,
                AnnouncementRecipient.employee_id == emp_id,
            )
            .exists()
        )
        visible_filter = no_recipients_subq | targeted_to_me_subq

        base_q = (
            session.query(Announcement, Employee.name)
            .outerjoin(Employee, Announcement.created_by == Employee.id)
            .filter(visible_filter)
            .order_by(
                Announcement.is_pinned.desc(),
                Announcement.created_at.desc(),
            )
        )

        total = session.query(Announcement).filter(visible_filter).count()
        rows = base_q.offset(skip).limit(limit).all()

        ann_ids = [ann.id for ann, _ in rows]
        read_ids = set()
        if ann_ids:
            read_ids = set(
                r.announcement_id
                for r in session.query(AnnouncementRead)
                .filter(
                    AnnouncementRead.employee_id == emp_id,
                    AnnouncementRead.announcement_id.in_(ann_ids),
                )
                .all()
            )

        items = []
        for ann, author_name in rows:
            items.append(
                {
                    "id": ann.id,
                    "title": ann.title,
                    "content": ann.content,
                    "priority": ann.priority,
                    "is_pinned": ann.is_pinned,
                    "created_by_name": author_name or "未知",
                    "created_at": (
                        ann.created_at.isoformat() if ann.created_at else None
                    ),
                    "is_read": ann.id in read_ids,
                }
            )

        response.headers["ETag"] = etag
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

        # F-009：補上可見性檢查（與 list 端 visible_filter 同步），
        # 並把「公告不存在」「不可見（指定 recipients 不含本人）」collapse 為
        # 同一 generic 403，避免透過 status code/detail 差異枚舉 Announcement id。
        no_recipients_subq = (
            ~session.query(AnnouncementRecipient)
            .filter(AnnouncementRecipient.announcement_id == Announcement.id)
            .exists()
        )
        targeted_to_me_subq = (
            session.query(AnnouncementRecipient)
            .filter(
                AnnouncementRecipient.announcement_id == Announcement.id,
                AnnouncementRecipient.employee_id == emp_id,
            )
            .exists()
        )
        visible_filter = no_recipients_subq | targeted_to_me_subq

        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id, visible_filter)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=403, detail="查無此公告或無權存取")

        existing = (
            session.query(AnnouncementRead)
            .filter(
                AnnouncementRead.announcement_id == announcement_id,
                AnnouncementRead.employee_id == emp_id,
            )
            .first()
        )

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
        raise_safe_500(e)
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

        no_recipients_subq = (
            ~session.query(AnnouncementRecipient)
            .filter(AnnouncementRecipient.announcement_id == Announcement.id)
            .exists()
        )
        targeted_to_me_subq = (
            session.query(AnnouncementRecipient)
            .filter(
                AnnouncementRecipient.announcement_id == Announcement.id,
                AnnouncementRecipient.employee_id == emp_id,
            )
            .exists()
        )
        visible_filter = no_recipients_subq | targeted_to_me_subq

        total = session.query(Announcement).filter(visible_filter).count()
        read = (
            session.query(AnnouncementRead)
            .filter(
                AnnouncementRead.employee_id == emp_id,
            )
            .count()
        )

        return {"unread_count": max(0, total - read)}
    finally:
        session.close()
