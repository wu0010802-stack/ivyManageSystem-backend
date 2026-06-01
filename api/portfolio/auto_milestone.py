"""Auto milestone detection API endpoint.

POST /api/students/{student_id}/milestones/auto-detect
  Body: optional { "reference_date": "YYYY-MM-DD" }
  Response: { "created_count": int, "skipped_existing": int, "total_detected": int }

Idempotent: 對每筆 payload 先 query 看是否已存在
(student_id, milestone_type, achieved_on, source_type, source_ref_type, source_ref_id);
有則 skip，無則 insert。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from utils.taipei_time import today_taipei
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from models.database import (
    Student,
    StudentAttendance,
    StudentMilestone,
    session_scope,
)
from services.milestone_detector import (
    detect_birthdays,
    detect_first_day,
    detect_graduation,
    detect_perfect_attendance_months,
)
from services.workday_rules import classify_day, load_day_rule_maps
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-auto-milestone"])


class AutoDetectPayload(BaseModel):
    reference_date: Optional[date] = None


def _milestone_dedup_query(session, payload: dict):
    """Look for existing (non-deleted) milestone matching the dedup key."""
    q = session.query(StudentMilestone).filter(
        StudentMilestone.student_id == payload["student_id"],
        StudentMilestone.milestone_type == payload["milestone_type"],
        StudentMilestone.achieved_on == payload["achieved_on"],
        StudentMilestone.source_type == payload["source_type"],
        StudentMilestone.deleted_at.is_(None),
    )
    src_type = payload.get("source_ref_type")
    src_id = payload.get("source_ref_id")
    if src_type is not None:
        q = q.filter(StudentMilestone.source_ref_type == src_type)
    else:
        q = q.filter(StudentMilestone.source_ref_type.is_(None))
    if src_id is not None:
        q = q.filter(StudentMilestone.source_ref_id == src_id)
    else:
        q = q.filter(StudentMilestone.source_ref_id.is_(None))
    return q


def _official_workdays_in_range(session, start: date, end: date) -> set[date]:
    """[start, end] 內的官方工作日（排除週末 / 假日、含補班日）。

    供全勤偵測算「該月應到天數」用；DB 依賴留在此層，milestone_detector 維持純函式。
    """
    if start > end:
        return set()
    holiday_map, makeup_map = load_day_rule_maps(session, start, end)
    workdays: set[date] = set()
    d = start
    while d <= end:
        if classify_day(d, holiday_map, makeup_map)["kind"] == "workday":
            workdays.add(d)
        d += timedelta(days=1)
    return workdays


@router.post("/{student_id}/milestones/auto-detect")
async def auto_detect_milestones(
    student_id: int,
    payload: Optional[AutoDetectPayload] = Body(default=None),
    request: Request = None,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    try:
        if payload is None:
            payload = AutoDetectPayload()
        ref_date = payload.reference_date or today_taipei()
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_WRITE.value)
            student = session.query(Student).filter(Student.id == student_id).first()
            if not student:
                raise HTTPException(status_code=404, detail="學生不存在")

            all_payloads: list[dict] = []
            all_payloads.extend(detect_first_day(student))
            all_payloads.extend(detect_birthdays(student, ref_date))
            all_payloads.extend(detect_graduation(student))

            att_rows = (
                session.query(StudentAttendance)
                .filter(StudentAttendance.student_id == student_id)
                .all()
            )
            att_records = [{"date": a.date, "status": a.status} for a in att_rows]
            if att_records:
                record_dates = [r["date"] for r in att_records]
                # 從第一筆記錄「所在月的月初」起算，使每個月的工作日集合都是整月，
                # 月中才有記錄的首月才能被正確判為非滿月全勤（不發章）。
                start = min(record_dates).replace(day=1)
                official_workdays = _official_workdays_in_range(
                    session, start, ref_date
                )
            else:
                official_workdays = set()
            all_payloads.extend(
                detect_perfect_attendance_months(
                    student_id, att_records, ref_date, official_workdays
                )
            )

            # created_by → employees.id；透過 User.employee_id 轉換（與 milestones router 一致）
            user_id = current_user.get("user_id")
            employee_id: int | None = None
            if user_id is not None:
                from models.database import User as _User

                u = session.query(_User).filter(_User.id == user_id).first()
                employee_id = u.employee_id if u else None

            created_count = 0
            skipped_existing = 0
            for p in all_payloads:
                existing = _milestone_dedup_query(session, p).first()
                if existing:
                    skipped_existing += 1
                    continue
                m = StudentMilestone(
                    student_id=p["student_id"],
                    milestone_type=p["milestone_type"],
                    achieved_on=p["achieved_on"],
                    title=p["title"],
                    description=p.get("description"),
                    icon=p.get("icon"),
                    source_type=p["source_type"],
                    source_ref_type=p.get("source_ref_type"),
                    source_ref_id=p.get("source_ref_id"),
                    created_by=employee_id,
                )
                # SAVEPOINT so a concurrent insert hitting uq_milestone_dedup just
                # bumps skipped_existing instead of poisoning the outer transaction.
                try:
                    with session.begin_nested():
                        session.add(m)
                        session.flush()
                    created_count += 1
                except IntegrityError:
                    skipped_existing += 1

            if request:
                request.state.audit_entity_id = str(student_id)
                request.state.audit_summary = (
                    f"自動偵測里程碑：student_id={student_id} "
                    f"created={created_count} skipped={skipped_existing}"
                )
            logger.info(
                "auto-detect milestones: student_id=%d created=%d skipped=%d operator=%s",
                student_id,
                created_count,
                skipped_existing,
                current_user.get("username"),
            )
            return {
                "created_count": created_count,
                "skipped_existing": skipped_existing,
                "total_detected": len(all_payloads),
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="自動偵測里程碑失敗")
