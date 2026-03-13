"""Admin notification center aggregation router."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from models.database import get_session
from services.dashboard_query_service import dashboard_query_service
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/summary")
def get_notification_summary(
    current_user: dict = Depends(get_current_user),
):
    """聚合後台待辦與提醒通知。"""
    if current_user.get("role") == "teacher":
        raise HTTPException(status_code=403, detail="教師帳號不可直接存取管理端 API")

    session = get_session()
    try:
        return dashboard_query_service.build_notification_summary(
            session,
            user_permissions=current_user.get("permissions", 0),
        )
    finally:
        session.close()
