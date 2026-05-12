"""Timeline aggregator router — 學生成長時間軸（多源合併）.

P2 V1：先支援 milestone source。後續 task 加上其他源。

路由：
- GET /api/students/{student_id}/timeline
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import StudentMilestone, session_scope
from services.timeline_aggregator import (
    SOURCE_TYPES,
    decode_cursor,
    milestone_to_timeline_item,
    sort_and_paginate,
)
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-timeline"])


def _parse_types(types: Optional[str]) -> set[str]:
    if not types:
        return set(SOURCE_TYPES)
    requested = {t.strip() for t in types.split(",") if t.strip()}
    return requested & set(SOURCE_TYPES)


def _fetch_milestones(session, student_id, since, until) -> list[dict]:
    q = session.query(StudentMilestone).filter(
        StudentMilestone.student_id == student_id,
        StudentMilestone.deleted_at.is_(None),
    )
    if since:
        q = q.filter(StudentMilestone.achieved_on >= since)
    if until:
        q = q.filter(StudentMilestone.achieved_on <= until)
    rows = q.order_by(StudentMilestone.achieved_on.desc()).limit(100).all()
    return [milestone_to_timeline_item(r) for r in rows]


def _by_type_count(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        t = it.get("type", "unknown")
        out[t] = out.get(t, 0) + 1
    return out


@router.get("/{student_id}/timeline")
async def get_timeline(
    student_id: int,
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    types: Optional[str] = Query(None, description="comma-separated source types"),
    cursor: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            requested_types = _parse_types(types)
            _ = decode_cursor(cursor)  # parsed but not yet used in single-source v1
            if not since:
                since = date.today() - timedelta(days=90)

            all_items: list[dict] = []

            if "milestone" in requested_types:
                all_items.extend(_fetch_milestones(session, student_id, since, until))
            # 後續 task 會在這加上其他來源

            paginated = sort_and_paginate(all_items, limit=limit)
            return {
                "items": paginated["items"],
                "next_cursor": paginated["next_cursor"],
                "available_types": list(SOURCE_TYPES),
                "stats": {
                    "total_items": len(all_items),
                    "by_type": _by_type_count(all_items),
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢時間軸失敗")
