"""
Audit log query router
"""

import logging
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from models.database import get_session, AuditLog
from utils.auth import require_permission
from utils.permissions import Permission
from sqlalchemy import desc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit-logs")
def get_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    username: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: dict = Depends(require_permission(Permission.AUDIT_LOGS)),
):
    """查詢操作審計紀錄"""
    session = get_session()
    try:
        q = session.query(AuditLog)

        if entity_type:
            q = q.filter(AuditLog.entity_type == entity_type)
        if action:
            q = q.filter(AuditLog.action == action)
        if username:
            q = q.filter(AuditLog.username.ilike(f"%{username}%"))
        if start_date:
            q = q.filter(AuditLog.created_at >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            q = q.filter(AuditLog.created_at <= datetime.combine(end_date, datetime.max.time()))

        total = q.count()
        items = (
            q.order_by(desc(AuditLog.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return {
            "items": [
                {
                    "id": log.id,
                    "user_id": log.user_id,
                    "username": log.username,
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "summary": log.summary,
                    "ip_address": log.ip_address,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in items
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()
