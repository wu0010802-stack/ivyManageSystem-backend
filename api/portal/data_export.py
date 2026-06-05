"""api/portal/data_export.py — 員工自身個資查閱權（個資法 §3.1）。

GET /api/portal/my-data-export

回當前員工自身完整資料 JSON：profile + 薪資歷史 + 考勤 + 請假 + 考核。
rate-limit 1/小時/user；50MB 上限（同家長端設計）。

稽核：寫 explicit audit。

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.2 員工 DSR
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Any

from fastapi.responses import Response
from pydantic import Field

from schemas._base import IvyBaseModel


class PortalMyDataExportOut(IvyBaseModel):
    """GET /portal/my-data-export 員工自身完整資料 JSON download.

    FastAPI 對 Response(content=...) bypass validation；本 schema 主要用於
    OpenAPI codegen 讓前端 type 對得上。多 nested 結構，用 dict[str, Any]
    彈性允許 backend 慢慢補強型別。
    """

    exported_at: str
    exported_by_user_id: int
    schema_version: int
    employee: dict[str, Any]
    salary_records: list[dict[str, Any]]
    attendance: list[dict[str, Any]]
    leaves: list[dict[str, Any]]
    overtimes: list[dict[str, Any]]
    appraisals: list[dict[str, Any]]


from models.database import get_session
from utils.audit import write_explicit_audit
from utils.auth import get_current_user
from utils.rate_limit import create_limiter
from utils.taipei_time import now_taipei_naive

from ._shared import _get_employee

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portal-data-export"])

_MAX_BYTES = 50 * 1024 * 1024

_export_limiter = create_limiter(
    max_calls=1,
    window_seconds=3600,
    name="portal_my_data_export",
    error_detail="每小時限下載 1 次，請稍後再試",
)


@router.get("/my-data-export", response_model=PortalMyDataExportOut)
def get_my_data_export(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """員工下載自身完整資料 JSON（個資法 §3.1 查閱複製權）。"""
    _export_limiter.check(f"user:{current_user['user_id']}")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_by_user_id": current_user["user_id"],
            "schema_version": 1,
            "employee": _collect_profile(emp),
            "salary_records": _collect_salary(session, emp.id),
            "attendance": _collect_attendance(session, emp.id),
            "leaves": _collect_leaves(session, emp.id),
            "overtimes": _collect_overtimes(session, emp.id),
            "appraisals": _collect_appraisals(session, emp.id),
        }
    finally:
        session.close()

    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body.encode("utf-8")) > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="資料量超過 50MB，請聯絡 HR 協助匯出",
        )

    filename = (
        f"ivy_portal_my_data_{current_user['user_id']}_"
        f"{now_taipei_naive().strftime('%Y%m%d')}.json"
    )

    write_explicit_audit(
        request,
        action="READ",
        entity_type="portal_my_data_export",
        entity_id=str(current_user["user_id"]),
        summary=f"員工下載自身資料 ({len(body)} bytes)",
        changes={
            "size_bytes": len(body),
            "is_full_bank_account": True,
        },
    )

    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── module helpers ────────────────────────────────────────────────────────


def _collect_profile(emp) -> dict:
    return {
        "id": emp.id,
        "name": emp.name,
        "id_number": emp.id_number,
        "phone": emp.phone,
        "email": emp.email,
        "address": emp.address,
        "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
        "termination_date": (emp.resign_date.isoformat() if emp.resign_date else None),
        "status": "在職" if emp.is_active else "離職",
        "bank_account": emp.bank_account,
        "emergency_contact_name": emp.emergency_contact_name,
        "emergency_contact_phone": emp.emergency_contact_phone,
    }


def _collect_salary(session, employee_id: int) -> list[dict]:
    try:
        from models.database import SalaryRecord

        rows = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.employee_id == employee_id)
            .order_by(SalaryRecord.salary_year.desc(), SalaryRecord.salary_month.desc())
            .all()
        )
        return [
            {
                "year": r.salary_year,
                "month": r.salary_month,
                "gross_salary": float(r.gross_salary) if r.gross_salary else 0,
                "net_salary": float(r.net_salary) if r.net_salary else 0,
                "base_salary": float(r.base_salary) if r.base_salary else 0,
                "is_finalized": r.is_finalized,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("salary export failed for emp %s", employee_id)
        return []


def _collect_attendance(session, employee_id: int) -> list[dict]:
    try:
        from models.database import Attendance

        rows = (
            session.query(Attendance)
            .filter(Attendance.employee_id == employee_id)
            .order_by(Attendance.attendance_date.asc())
            .all()
        )
        return [
            {
                "date": r.attendance_date.isoformat() if r.attendance_date else None,
                "punch_in": r.punch_in_time.isoformat() if r.punch_in_time else None,
                "punch_out": r.punch_out_time.isoformat() if r.punch_out_time else None,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("attendance export failed for emp %s", employee_id)
        return []


def _collect_leaves(session, employee_id: int) -> list[dict]:
    try:
        from models.leave import LeaveRecord

        rows = (
            session.query(LeaveRecord)
            .filter(LeaveRecord.employee_id == employee_id)
            .order_by(LeaveRecord.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "leave_type": r.leave_type,
                "start_at": r.start_date.isoformat() if r.start_date else None,
                "end_at": r.end_date.isoformat() if r.end_date else None,
                "hours": float(r.leave_hours) if r.leave_hours else 0,
                "reason": r.reason,
                "status": r.status,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("leaves export failed for emp %s", employee_id)
        return []


def _collect_overtimes(session, employee_id: int) -> list[dict]:
    try:
        from models.overtime import OvertimeRecord

        rows = (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.employee_id == employee_id)
            .order_by(OvertimeRecord.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "start_at": r.start_time.isoformat() if r.start_time else None,
                "end_at": r.end_time.isoformat() if r.end_time else None,
                "hours": float(r.hours) if r.hours else 0,
                "reason": r.reason,
                "status": r.status,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("overtimes export failed for emp %s", employee_id)
        return []


def _collect_appraisals(session, employee_id: int) -> list[dict]:
    try:
        from models.appraisal import AppraisalSummary, AppraisalParticipant

        rows = (
            session.query(AppraisalSummary)
            .join(
                AppraisalParticipant,
                AppraisalParticipant.id == AppraisalSummary.participant_id,
            )
            .filter(AppraisalParticipant.employee_id == employee_id)
            .order_by(AppraisalSummary.id.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "cycle_id": r.cycle_id,
                "total_score": float(r.total_score) if r.total_score else 0,
                "bonus_amount": float(r.bonus_amount) if r.bonus_amount else 0,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("appraisals export failed for emp %s", employee_id)
        return []
