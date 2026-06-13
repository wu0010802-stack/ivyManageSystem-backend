"""api/salary/enrollment_snapshot.py — 月度在籍人數快照（L2）。

4 個 endpoint：
- GET   /salaries/enrollment-snapshot?year&month   檢視（含班名、涵蓋月資訊）
- POST  /salaries/enrollment-snapshot/generate     產生/重產（回 diff）
- PATCH /salaries/enrollment-snapshot/{id}         手調人數（reason ≥10 字）
- POST  /salaries/enrollment-snapshot/confirm      確認該月全部列

權限：讀 SALARY_READ、寫 SALARY_WRITE。手調/重產/確認都會寫 audit；
手調與重產值變動會標記受影響發放月薪資 needs_recalc。受影響發放月已封存
→ 409（同 finalize 守衛語意）。

Refs: docs/superpowers/specs/2026-06-13-enrollment-count-correctness-design.md
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from models.base import session_scope
from models.database import Classroom, SalaryRecord
from models.enrollment_snapshot import ClassEnrollmentSnapshot
from services.salary.enrollment_snapshot import generate_snapshot
from services.salary.utils import (
    get_distribution_period_months,
    mark_salary_stale_for_enrollment_event,
    next_distribution_month,
)
from utils.auth import require_staff_permission
from utils.finance_guards import require_adjustment_reason
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

router = APIRouter()


class SnapshotGenerateRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    force: bool = Field(False, description="True 連已確認列一併覆寫")


class SnapshotPatchRequest(BaseModel):
    student_count: float = Field(..., ge=0, le=99999)
    reason: Optional[str] = Field(None, description="手調原因（必填 ≥10 字）")


class SnapshotConfirmRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)


def _assert_distribution_month_not_finalized(session, year: int, month: int) -> None:
    """該月人數會進的發放月若已有封存薪資 → 409（人數已成定案依據）。"""
    dist_year, dist_month = next_distribution_month(year, month)
    finalized = (
        session.query(SalaryRecord.id)
        .filter(
            SalaryRecord.salary_year == dist_year,
            SalaryRecord.salary_month == dist_month,
            SalaryRecord.is_finalized.is_(True),
        )
        .first()
    )
    if finalized:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{year} 年 {month} 月人數會進 {dist_year} 年 {dist_month} 月"
                "發放月，該月薪資已封存，不可再變更人數快照。請先解除封存。"
            ),
        )


def _num(value):
    f = float(value or 0)
    return int(f) if f == int(f) else f


@router.get("/salaries/enrollment-snapshot")
def get_enrollment_snapshot(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """檢視該月快照列（含班名）。發放月時附 covered_months 供前端展開涵蓋月。"""
    with session_scope() as session:
        rows = (
            session.query(ClassEnrollmentSnapshot)
            .filter(
                ClassEnrollmentSnapshot.snapshot_year == year,
                ClassEnrollmentSnapshot.snapshot_month == month,
            )
            .order_by(ClassEnrollmentSnapshot.classroom_id.asc().nullsfirst())
            .all()
        )
        classroom_names = {
            cid: name for cid, name in session.query(Classroom.id, Classroom.name)
        }
        return {
            "year": year,
            "month": month,
            "exists": bool(rows),
            "covered_months": get_distribution_period_months(year, month),
            "rows": [
                {
                    "id": row.id,
                    "classroom_id": row.classroom_id,
                    "classroom_name": (
                        "全校"
                        if row.classroom_id is None
                        else classroom_names.get(
                            row.classroom_id, f"#{row.classroom_id}"
                        )
                    ),
                    "student_count": _num(row.student_count),
                    "count_mode": row.count_mode,
                    "is_confirmed": bool(row.is_confirmed),
                    "confirmed_by": row.confirmed_by,
                    "adjust_reason": row.adjust_reason,
                    "updated_by": row.updated_by,
                    "generated_at": (
                        row.generated_at.isoformat() if row.generated_at else None
                    ),
                }
                for row in rows
            ],
        }


@router.post("/salaries/enrollment-snapshot/generate")
def generate_enrollment_snapshot(
    data: SnapshotGenerateRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """產生/重產該月快照。值有變動時標記受影響發放月薪資需重算。"""
    from utils.audit import write_explicit_audit

    with session_scope() as session:
        _assert_distribution_month_not_finalized(session, data.year, data.month)
        result = generate_snapshot(
            session,
            data.year,
            data.month,
            updated_by=current_user.get("username"),
            force=data.force,
        )
        if result["changes"]:
            mark_salary_stale_for_enrollment_event(
                session, date(data.year, data.month, 1)
            )
        write_explicit_audit(
            request,
            action="UPDATE",
            entity_type="enrollment_snapshot",
            entity_id=f"{data.year}-{data.month:02d}",
            summary=(
                f"產生在籍人數快照 {data.year}-{data.month:02d}"
                f"（{len(result['changes'])} 列變動）"
            ),
            changes={"changes": result["changes"], "force": data.force},
        )
        return {
            "message": "快照已產生",
            "generated": result["generated"],
            "changes": result["changes"],
        }


@router.patch("/salaries/enrollment-snapshot/{snapshot_id}")
def patch_enrollment_snapshot(
    snapshot_id: int,
    data: SnapshotPatchRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """手調單列人數（需原因 ≥10 字）；視為已確認並標記薪資需重算。"""
    from utils.audit import write_explicit_audit

    cleaned_reason = require_adjustment_reason(data.reason)
    with session_scope() as session:
        row = session.get(ClassEnrollmentSnapshot, snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail="找不到快照列")
        _assert_distribution_month_not_finalized(
            session, row.snapshot_year, row.snapshot_month
        )
        old_value = _num(row.student_count)
        row.student_count = data.student_count
        row.count_mode = "manual"
        row.adjust_reason = cleaned_reason
        row.is_confirmed = True
        row.confirmed_by = current_user.get("username")
        row.confirmed_at = now_taipei_naive()
        row.updated_by = current_user.get("username")
        mark_salary_stale_for_enrollment_event(
            session, date(row.snapshot_year, row.snapshot_month, 1)
        )
        write_explicit_audit(
            request,
            action="UPDATE",
            entity_type="enrollment_snapshot",
            entity_id=str(snapshot_id),
            summary=(
                f"手調在籍人數 {row.snapshot_year}-{row.snapshot_month:02d}"
                f" classroom={row.classroom_id or '全校'}："
                f"{old_value} → {_num(data.student_count)}"
            ),
            changes={
                "before": old_value,
                "after": _num(data.student_count),
                "reason": cleaned_reason,
            },
        )
        return {
            "message": "已更新",
            "before": old_value,
            "after": _num(data.student_count),
        }


@router.post("/salaries/enrollment-snapshot/confirm")
def confirm_enrollment_snapshot(
    data: SnapshotConfirmRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """確認該月全部快照列（重產時不再被自動覆寫）。"""
    from utils.audit import write_explicit_audit

    with session_scope() as session:
        rows = (
            session.query(ClassEnrollmentSnapshot)
            .filter(
                ClassEnrollmentSnapshot.snapshot_year == data.year,
                ClassEnrollmentSnapshot.snapshot_month == data.month,
            )
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail="該月尚未產生快照")
        username = current_user.get("username")
        now = now_taipei_naive()
        for row in rows:
            row.is_confirmed = True
            row.confirmed_by = username
            row.confirmed_at = now
        write_explicit_audit(
            request,
            action="UPDATE",
            entity_type="enrollment_snapshot",
            entity_id=f"{data.year}-{data.month:02d}",
            summary=f"確認在籍人數快照 {data.year}-{data.month:02d}（{len(rows)} 列）",
            changes={"confirmed_rows": len(rows)},
        )
        return {"message": "已確認", "confirmed": len(rows)}
