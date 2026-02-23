"""
Approval summary router - pending counts for dashboard
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from models.database import get_session, LeaveRecord, OvertimeRecord
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["approvals"])


@router.get("/approval-summary")
def get_approval_summary(
    current_user: dict = Depends(require_permission(Permission.APPROVALS)),
):
    """取得待審核項目數量"""
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
