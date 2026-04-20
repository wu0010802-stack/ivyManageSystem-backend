"""
api/activity/inquiries.py — 家長提問端點（4 個）
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, ParentInquiry
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission

from ._shared import _not_found, _invalidate_activity_dashboard_caches, InquiryReply

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/inquiries")
async def get_inquiries(
    is_read: bool = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得家長提問列表"""
    session = get_session()
    try:
        q = session.query(ParentInquiry)
        if is_read is not None:
            q = q.filter(ParentInquiry.is_read.is_(is_read))
        total = q.count()
        rows = (
            q.order_by(ParentInquiry.created_at.desc()).offset(skip).limit(limit).all()
        )
        items = [
            {
                "id": r.id,
                "name": r.name,
                "phone": r.phone,
                "question": r.question,
                "is_read": r.is_read,
                "reply": r.reply,
                "replied_at": r.replied_at.isoformat() if r.replied_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


@router.put("/inquiries/{inquiry_id}/read")
async def mark_inquiry_read(
    inquiry_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """標記提問為已讀"""
    session = get_session()
    try:
        inquiry = (
            session.query(ParentInquiry).filter(ParentInquiry.id == inquiry_id).first()
        )
        if not inquiry:
            raise _not_found("提問")
        inquiry.is_read = True
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": "已標記為已讀"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/inquiries/{inquiry_id}/reply")
async def reply_inquiry(
    inquiry_id: int,
    body: InquiryReply,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """回覆家長提問"""
    session = get_session()
    try:
        inquiry = (
            session.query(ParentInquiry).filter(ParentInquiry.id == inquiry_id).first()
        )
        if not inquiry:
            raise _not_found("提問")
        inquiry.reply = body.reply.strip()
        inquiry.replied_at = datetime.now()
        inquiry.is_read = True  # 回覆同時自動標記已讀
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": "回覆成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/inquiries/{inquiry_id}")
async def delete_inquiry(
    inquiry_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """刪除提問"""
    session = get_session()
    try:
        inquiry = (
            session.query(ParentInquiry).filter(ParentInquiry.id == inquiry_id).first()
        )
        if not inquiry:
            raise _not_found("提問")
        session.delete(inquiry)
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
