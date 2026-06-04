"""
api/salary/detail.py — 單筆薪資查詢 / 匯出

含 4 個 endpoint + snapshot 快取（TTL 60s）：
- GET /salaries/{record_id}/audit-log         操作歷史
- GET /salaries/{record_id}/breakdown         明細
- GET /salaries/{record_id}/field-breakdown   單欄位明細（吃 snapshot 快取）
- GET /salaries/{record_id}/export            單人薪資單 PDF

所有 symbol 僅本模組內使用，不需 re-export。
_salary_engine 採 endpoint 內 lazy import 取最新值。
"""

import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import joinedload

from models.base import session_scope
from models.database import Employee, SalaryRecord
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.salary_field_breakdown import (
    FIELD_LABELS,
    build_field_breakdown,
    build_salary_debug_snapshot,
)
from utils.auth import require_staff_permission
from utils.cache_layer import get_cache
from utils.error_messages import SALARY_RECORD_NOT_FOUND
from schemas.salary_detail import (
    SalaryDetailAuditLogOut,
    SalaryDetailBreakdownOut,
    SalaryDetailFieldBreakdownOut,
    SalaryDetailUnusedLeavePayoutOut,
)
from utils.permissions import Permission
from utils.salary_access import (
    enforce_self_or_full_salary as _enforce_self_or_full_salary,
)

router = APIRouter()


# ── 薪資 debug snapshot 快取 ─────────────────────────────────────────────
# 同一筆薪資在 UI 切換不同欄位時，snapshot 內容不變（~13 個 DB 查詢）。
# 用 record_id 為 key、(version, data) 為 value，版本更動即失效避免陳舊資料。
# scope: global（snapshot 本身不含 PII 跨 user，且 record_id 已 unique）
_CACHE_NS_SALARY_SNAPSHOT = "salary_snapshot"
_CACHE_TTL_SALARY_SNAPSHOT = 60  # 1 分鐘


def _snapshot_cache_get(record_id: int, version: int):
    entry = get_cache().get(_CACHE_NS_SALARY_SNAPSHOT, str(record_id))
    if entry is None:
        return None
    cached_version, data = entry
    if cached_version != version:
        # version mismatch：失效該筆。worst case 兩 thread 都 delete（idempotent）
        get_cache().delete(_CACHE_NS_SALARY_SNAPSHOT, str(record_id))
        return None
    return data


def _snapshot_cache_put(record_id: int, version: int, data: dict) -> None:
    get_cache().set(
        _CACHE_NS_SALARY_SNAPSHOT,
        str(record_id),
        (version, data),
        ttl=_CACHE_TTL_SALARY_SNAPSHOT,
    )


@router.get("/salaries/{record_id}/audit-log", response_model=SalaryDetailAuditLogOut)
def get_salary_audit_log(
    record_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資的操作歷史（來源：通用 AuditLog 表）。"""
    from models.audit import AuditLog
    from sqlalchemy import desc

    from utils.audit import write_explicit_audit

    with session_scope() as session:
        record_owner = (
            session.query(SalaryRecord.employee_id)
            .filter(SalaryRecord.id == record_id)
            .scalar()
        )
        if record_owner is None:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        _enforce_self_or_full_salary(current_user, record_owner)

        logs = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "salary",
                AuditLog.entity_id == str(record_id),
            )
            .order_by(desc(AuditLog.created_at))
            .limit(limit)
            .all()
        )
        result = {
            "record_id": record_id,
            "items": [
                {
                    "id": log.id,
                    "action": log.action,
                    "username": log.username,
                    "summary": log.summary,
                    "created_at": (
                        log.created_at.isoformat() if log.created_at else None
                    ),
                }
                for log in logs
            ],
        }
        write_explicit_audit(
            request,
            action="READ",
            entity_type="salary",
            entity_id=str(record_id),
            summary=f"查看薪資自身稽核：record={record_id}",
            changes={"record_id": record_id, "items_returned": len(logs)},
        )
        return result


@router.get("/salaries/{record_id}/breakdown", response_model=SalaryDetailBreakdownOut)
def get_salary_breakdown(
    record_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資明細。"""
    from utils.audit import write_explicit_audit

    with session_scope() as session:
        record, emp = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(joinedload(Employee.job_title_rel)).filter(
            SalaryRecord.id == record_id
        ).first() or (
            None,
            None,
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        _enforce_self_or_full_salary(current_user, record.employee_id)

        job_title = ""
        if emp.job_title_rel:
            job_title = emp.job_title_rel.name
        elif emp.title:
            job_title = emp.title

        result = {
            "employee": {
                "record_id": record.id,
                "employee_name": emp.name,
                "employee_code": emp.employee_id,
                "job_title": job_title,
                "year": record.salary_year,
                "month": record.salary_month,
            },
            "earnings": {
                "base_salary": record.base_salary or 0,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "overtime_pay": record.overtime_pay or 0,
                "extra_allowance": record.extra_allowance or 0,
                "extra_allowance_label": record.extra_allowance_label,
                "gross_salary": record.gross_salary or 0,
            },
            "bonuses": {
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "birthday_bonus": record.birthday_bonus or 0,
            },
            "deductions": {
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "labor_insurance": record.labor_insurance_employee or 0,
                "health_insurance": record.health_insurance_employee or 0,
                "supplementary_health_employee": (
                    record.supplementary_health_employee or 0
                ),
                "pension": record.pension_employee or 0,
                "total_deduction": record.total_deduction or 0,
            },
            "summary": {
                "net_salary": record.net_salary or 0,
                "bonus_separate": bool(record.bonus_separate),
                "bonus_amount": record.bonus_amount or 0,
            },
            "manual_overrides": list(record.manual_overrides or []),
        }
        write_explicit_audit(
            request,
            action="READ",
            entity_type="salary",
            entity_id=str(record_id),
            summary=f"查看薪資明細：record={record_id}",
            changes={"record_id": record_id},
        )
        return result


@router.get(
    "/salaries/{record_id}/field-breakdown",
    response_model=SalaryDetailFieldBreakdownOut,
)
def get_salary_field_breakdown(
    record_id: int,
    request: Request,
    field: str = Query(...),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資指定欄位明細。"""
    # Lazy import _salary_engine：service injection 後才有值。
    from . import _salary_engine

    from utils.audit import write_explicit_audit

    if field not in FIELD_LABELS:
        raise HTTPException(status_code=400, detail="不支援的明細欄位")

    with session_scope() as session:
        record, emp = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(joinedload(Employee.job_title_rel)).filter(
            SalaryRecord.id == record_id
        ).first() or (
            None,
            None,
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        _enforce_self_or_full_salary(current_user, record.employee_id)

        engine = (
            _salary_engine
            if _salary_engine
            else RuntimeSalaryEngine(load_from_db=False)
        )
        version = int(record.version or 1)
        snapshot = _snapshot_cache_get(record_id, version)
        if snapshot is None:
            snapshot = build_salary_debug_snapshot(
                session, engine, emp, record.salary_year, record.salary_month
            )
            _snapshot_cache_put(record_id, version, snapshot)
        result = build_field_breakdown(record, emp, snapshot, field)
        write_explicit_audit(
            request,
            action="READ",
            entity_type="salary",
            entity_id=str(record_id),
            summary=f"查看薪資欄位拆分：record={record_id}",
            changes={"record_id": record_id, "field": field},
        )
        return result


@router.get(
    "/salaries/{record_id}/unused-leave-payout-detail",
    response_model=SalaryDetailUnusedLeavePayoutOut,
)
def get_unused_leave_payout_detail(
    record_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資的未休假折算工資明細。

    權限：HR（SALARY_READ + admin/hr role）或員工本人（自己的薪資記錄）。

    回傳欄位：
    - salary_record_id：薪資記錄 id
    - employee_id：員工 id
    - total_amount：未休假折算總額（= SalaryRecord.unused_leave_payout）
    - logs：各 source_type 明細列表
    """
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    with session_scope() as session:
        sr = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        if sr is None:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        _enforce_self_or_full_salary(current_user, sr.employee_id)

        logs = (
            session.query(UnusedLeavePayoutLog)
            .filter(UnusedLeavePayoutLog.salary_record_id == record_id)
            .order_by(UnusedLeavePayoutLog.created_at)
            .all()
        )

        return {
            "salary_record_id": sr.id,
            "employee_id": sr.employee_id,
            "total_amount": float(sr.unused_leave_payout or 0),
            "logs": [
                {
                    "log_id": log.id,
                    "source_type": log.source_type,
                    "hours": log.hours,
                    "hourly_wage": float(log.hourly_wage),
                    "amount": float(log.amount),
                    "wage_basis_date": log.wage_basis_date.isoformat(),
                    "meta": log.meta or {},
                }
                for log in logs
            ],
        }


@router.get("/salaries/{record_id}/export")
def export_salary_slip(
    record_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    format: str = Query("pdf", pattern="^(pdf)$"),
):
    """匯出單人薪資單 PDF"""
    from services.salary_slip import generate_salary_pdf

    with session_scope() as session:
        result = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.id == record_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        record, emp = result

        _enforce_self_or_full_salary(current_user, record.employee_id)

        pdf_bytes = generate_salary_pdf(
            record, emp, record.salary_year, record.salary_month
        )

        filename = (
            f"salary_{emp.name}_{record.salary_year}_{record.salary_month:02d}.pdf"
        )
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
