"""經營分析 API。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.base import session_scope
from services.analytics.churn_service import (
    build_churn_history,
    detect_at_risk_students,
)
from services.analytics.funnel_service import build_funnel
from services.report_cache_service import report_cache_service
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

FUNNEL_TTL = 1800  # 30 min
AT_RISK_TTL = 300  # 5 min
CHURN_HISTORY_TTL = 3600  # 1 hr

# NOTE: 目前僅靠 TTL 失效。學生 lifecycle 轉移與學費繳清等變動點未主動 invalidate，
# 因此 dashboard 在 TTL 內可能仍顯示已處理的預警。可接受 MVP 折衷。


@router.get("/funnel")
def get_funnel(
    start: date = Query(..., description="區間起（含）"),
    end: date = Query(..., description="區間迄（含）"),
    grade: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    current_user: dict = Depends(
        require_staff_permission(Permission.BUSINESS_ANALYTICS)
    ),
):
    if start > end:
        raise HTTPException(400, "start 必須 ≤ end")
    today = date.today()
    with session_scope() as session:
        return report_cache_service.get_or_build(
            session,
            category="analytics_funnel",
            ttl_seconds=FUNNEL_TTL,
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "grade": grade,
                "source": source,
                "today": today.isoformat(),
            },
            builder=lambda: build_funnel(
                session,
                start_date=start,
                end_date=end,
                today=today,
                grade_filter=grade,
                source_filter=source,
            ),
        )


@router.get("/churn/at-risk")
def get_at_risk(
    current_user: dict = Depends(
        require_staff_permission(Permission.BUSINESS_ANALYTICS)
    ),
):
    today = date.today()
    can_read = bool(
        int(current_user.get("permissions", 0)) & int(Permission.STUDENTS_READ)
    )
    with session_scope() as session:
        return report_cache_service.get_or_build(
            session,
            category="analytics_churn_at_risk",
            ttl_seconds=AT_RISK_TTL,
            params={
                "today": today.isoformat(),
                "can_read": can_read,
            },
            builder=lambda: detect_at_risk_students(
                session,
                today=today,
                can_read_students=can_read,
            ),
        )


@router.get("/churn/history")
def get_churn_history(
    months: int = Query(12, ge=1, le=36),
    current_user: dict = Depends(
        require_staff_permission(Permission.BUSINESS_ANALYTICS)
    ),
):
    today = date.today()
    with session_scope() as session:
        return report_cache_service.get_or_build(
            session,
            category="analytics_churn_history",
            ttl_seconds=CHURN_HISTORY_TTL,
            params={"months": months, "today": today.isoformat()},
            builder=lambda: build_churn_history(
                session,
                months=months,
                today=today,
            ),
        )
