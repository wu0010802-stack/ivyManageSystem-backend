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

from models.database import (
    StudentAssessment,
    StudentAttendance,
    StudentContactBookEntry,
    StudentIncident,
    StudentMeasurement,
    StudentMilestone,
    StudentObservation,
    session_scope,
)
from models.student_log import ParentCommunicationLog
from services.timeline_aggregator import (
    SOURCE_TYPES,
    assessment_to_timeline_item,
    communication_to_timeline_item,
    decode_cursor,
    incident_to_timeline_item,
    measurement_to_timeline_item,
    milestone_to_timeline_item,
    observation_to_timeline_item,
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


def _fetch_measurements(session, student_id, since, until) -> list[dict]:
    q = session.query(StudentMeasurement).filter(
        StudentMeasurement.student_id == student_id
    )
    if since:
        q = q.filter(StudentMeasurement.measured_on >= since)
    if until:
        q = q.filter(StudentMeasurement.measured_on <= until)
    rows = q.order_by(StudentMeasurement.measured_on.desc()).limit(100).all()
    return [measurement_to_timeline_item(r) for r in rows]


def _fetch_observations(session, student_id, since, until) -> list[dict]:
    q = session.query(StudentObservation).filter(
        StudentObservation.student_id == student_id,
        StudentObservation.deleted_at.is_(None),
    )
    if since:
        q = q.filter(StudentObservation.observation_date >= since)
    if until:
        q = q.filter(StudentObservation.observation_date <= until)
    rows = q.order_by(StudentObservation.observation_date.desc()).limit(100).all()
    return [observation_to_timeline_item(r) for r in rows]


def _fetch_assessments(session, student_id, since, until) -> list[dict]:
    q = session.query(StudentAssessment).filter(
        StudentAssessment.student_id == student_id
    )
    if since:
        q = q.filter(StudentAssessment.assessment_date >= since)
    if until:
        q = q.filter(StudentAssessment.assessment_date <= until)
    rows = q.order_by(StudentAssessment.assessment_date.desc()).limit(100).all()
    return [assessment_to_timeline_item(r) for r in rows]


def _fetch_incidents(session, student_id, since, until) -> list[dict]:
    q = session.query(StudentIncident).filter(StudentIncident.student_id == student_id)
    if since:
        q = q.filter(StudentIncident.occurred_at >= since)
    if until:
        q = q.filter(StudentIncident.occurred_at <= until)
    rows = q.order_by(StudentIncident.occurred_at.desc()).limit(100).all()
    return [incident_to_timeline_item(r) for r in rows]


def _fetch_communications(session, student_id, since, until) -> list[dict]:
    q = session.query(ParentCommunicationLog).filter(
        ParentCommunicationLog.student_id == student_id
    )
    if since:
        q = q.filter(ParentCommunicationLog.communication_date >= since)
    if until:
        q = q.filter(ParentCommunicationLog.communication_date <= until)
    rows = q.order_by(ParentCommunicationLog.communication_date.desc()).limit(100).all()
    return [communication_to_timeline_item(r) for r in rows]


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
            if "measurement" in requested_types:
                all_items.extend(_fetch_measurements(session, student_id, since, until))
            if "observation" in requested_types:
                all_items.extend(_fetch_observations(session, student_id, since, until))
            if "assessment" in requested_types:
                all_items.extend(_fetch_assessments(session, student_id, since, until))
            if "incident" in requested_types:
                all_items.extend(_fetch_incidents(session, student_id, since, until))
            if "communication" in requested_types:
                all_items.extend(
                    _fetch_communications(session, student_id, since, until)
                )

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
