"""api/parent_portal/timeline.py — 家長端的學生成長時間軸（read-only）

端點：
- GET /api/parent/timeline?student_id=&since=&until=&types=&cursor=&limit=

權限：require_parent_role + IDOR (學生屬該家長)。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.activity import ActivityRegistration
from models.database import (
    StudentAssessment,
    StudentAttendance,
    StudentContactBookEntry,
    StudentIncident,
    StudentMeasurement,
    StudentMilestone,
    StudentObservation,
)
from models.student_log import ParentCommunicationLog
from services.timeline_aggregator import (
    SOURCE_TYPES,
    activity_to_timeline_item,
    assessment_to_timeline_item,
    attendance_to_timeline_item,
    communication_to_timeline_item,
    contact_book_to_timeline_item,
    decode_cursor,
    incident_to_timeline_item,
    measurement_to_timeline_item,
    milestone_to_timeline_item,
    observation_to_timeline_item,
    sort_and_paginate,
)
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._dependencies import get_parent_db
from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/timeline", tags=["parent-timeline"])


def _parse_types(types: Optional[str]) -> set[str]:
    if not types:
        return set(SOURCE_TYPES)
    requested = {t.strip() for t in types.split(",") if t.strip()}
    return requested & set(SOURCE_TYPES)


def _by_type_count(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        t = it.get("type", "unknown")
        out[t] = out.get(t, 0) + 1
    return out


@router.get("")
async def parent_get_timeline(
    student_id: int = Query(..., description="學生 id"),
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    types: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> dict:
    try:
        user_id = current_user["user_id"]
        requested_types = _parse_types(types)
        # Multi-source cursor pagination is TBD; signal "no more pages" rather than
        # re-fetching head and looping the parent app (it drives loadMore on cursor).
        if decode_cursor(cursor):
            _assert_student_owned(session, user_id, student_id)
            return {
                "items": [],
                "next_cursor": None,
                "stats": {"total_items": 0, "by_type": {}},
            }
        if not since:
            since = date.today() - timedelta(days=90)  # noqa: DTZ011

        _assert_student_owned(session, user_id, student_id)
        all_items: list[dict] = []

        if "milestone" in requested_types:
            q = session.query(StudentMilestone).filter(
                StudentMilestone.student_id == student_id,
                StudentMilestone.deleted_at.is_(None),
            )
            if since:
                q = q.filter(StudentMilestone.achieved_on >= since)
            if until:
                q = q.filter(StudentMilestone.achieved_on <= until)
            all_items.extend(
                milestone_to_timeline_item(r)
                for r in q.order_by(StudentMilestone.achieved_on.desc())
                .limit(100)
                .all()
            )

        if "measurement" in requested_types:
            q = session.query(StudentMeasurement).filter(
                StudentMeasurement.student_id == student_id
            )
            if since:
                q = q.filter(StudentMeasurement.measured_on >= since)
            if until:
                q = q.filter(StudentMeasurement.measured_on <= until)
            all_items.extend(
                measurement_to_timeline_item(r)
                for r in q.order_by(StudentMeasurement.measured_on.desc())
                .limit(100)
                .all()
            )

        if "observation" in requested_types:
            q = session.query(StudentObservation).filter(
                StudentObservation.student_id == student_id,
                StudentObservation.deleted_at.is_(None),
            )
            if since:
                q = q.filter(StudentObservation.observation_date >= since)
            if until:
                q = q.filter(StudentObservation.observation_date <= until)
            all_items.extend(
                observation_to_timeline_item(r)
                for r in q.order_by(StudentObservation.observation_date.desc())
                .limit(100)
                .all()
            )

        if "assessment" in requested_types:
            q = session.query(StudentAssessment).filter(
                StudentAssessment.student_id == student_id
            )
            if since:
                q = q.filter(StudentAssessment.assessment_date >= since)
            if until:
                q = q.filter(StudentAssessment.assessment_date <= until)
            all_items.extend(
                assessment_to_timeline_item(r)
                for r in q.order_by(StudentAssessment.assessment_date.desc())
                .limit(100)
                .all()
            )

        if "incident" in requested_types:
            # 與 admin 一致：用 created_at 作 occurred_at
            q = session.query(StudentIncident).filter(
                StudentIncident.student_id == student_id
            )
            if since:
                q = q.filter(StudentIncident.created_at >= since)
            if until:
                q = q.filter(StudentIncident.created_at <= until)
            all_items.extend(
                incident_to_timeline_item(r)
                for r in q.order_by(StudentIncident.created_at.desc()).limit(100).all()
            )

        if "communication" in requested_types:
            q = session.query(ParentCommunicationLog).filter(
                ParentCommunicationLog.student_id == student_id
            )
            if since:
                q = q.filter(ParentCommunicationLog.communication_date >= since)
            if until:
                q = q.filter(ParentCommunicationLog.communication_date <= until)
            all_items.extend(
                communication_to_timeline_item(r)
                for r in q.order_by(ParentCommunicationLog.communication_date.desc())
                .limit(100)
                .all()
            )

        if "contact_book" in requested_types:
            # round 5 P1：家長端 timeline 不應吐老師草稿/軟刪。
            q = session.query(StudentContactBookEntry).filter(
                StudentContactBookEntry.student_id == student_id,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
            )
            if since:
                q = q.filter(StudentContactBookEntry.log_date >= since)
            if until:
                q = q.filter(StudentContactBookEntry.log_date <= until)
            all_items.extend(
                contact_book_to_timeline_item(r)
                for r in q.order_by(StudentContactBookEntry.log_date.desc())
                .limit(100)
                .all()
            )

        if "attendance" in requested_types:
            q = session.query(StudentAttendance).filter(
                StudentAttendance.student_id == student_id,
                StudentAttendance.status != "出席",
            )
            if since:
                q = q.filter(StudentAttendance.date >= since)
            if until:
                q = q.filter(StudentAttendance.date <= until)
            all_items.extend(
                attendance_to_timeline_item(r)
                for r in q.order_by(StudentAttendance.date.desc()).limit(100).all()
            )

        if "activity" in requested_types:
            q = session.query(ActivityRegistration).filter(
                ActivityRegistration.student_id == student_id
            )
            if since:
                q = q.filter(ActivityRegistration.created_at >= since)
            if until:
                q = q.filter(ActivityRegistration.created_at <= until)
            all_items.extend(
                activity_to_timeline_item(r)
                for r in q.order_by(ActivityRegistration.created_at.desc())
                .limit(100)
                .all()
            )

        paginated = sort_and_paginate(all_items, limit=limit)
        return {
            "items": paginated["items"],
            "next_cursor": paginated["next_cursor"],
            "stats": {
                "total_items": len(all_items),
                "by_type": _by_type_count(all_items),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端時間軸查詢失敗")
