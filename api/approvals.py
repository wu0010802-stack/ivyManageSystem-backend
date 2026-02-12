"""
Approval summary router - pending counts for dashboard
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from models.database import get_session, LeaveRecord, OvertimeRecord
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["approvals"])


@router.get("/approval-summary")
def get_approval_summary(
    current_user: dict = Depends(get_current_user),
):
    """取得待審核項目數量"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="僅限管理員操作")

    session = get_session()
    try:
        pending_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.is_approved.is_(None),
        ).count()

        pending_overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.is_approved.is_(None),
        ).count()

        return {
            "pending_leaves": pending_leaves,
            "pending_overtimes": pending_overtimes,
            "total": pending_leaves + pending_overtimes,
        }
    finally:
        session.close()
