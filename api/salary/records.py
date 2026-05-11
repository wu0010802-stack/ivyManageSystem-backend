"""
api/salary/records.py — 薪資列表 / 歷史 / 全員匯出

含 4 個 endpoint：
- GET /salaries/records         月薪資列表（支援分頁與 viewer 過濾）
- GET /salaries/export-all      整月薪資 xlsx/pdf 匯出（admin/hr）
- GET /salaries/history         單員工歷史薪資
- GET /salaries/history-all     年度全員薪資概覽（依員工分組分頁）

所有 symbol 僅本模組內使用，不需 re-export。
records 端點呼叫的 _trigger_past_month_snapshot_if_missing 採 lazy import
保留在 __init__.py，讓 test_salary_snapshot 對 _today_taipei 的
monkeypatch 仍生效。
"""

import io

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import joinedload

from api.salary_fields import calculate_display_bonus_total
from models.base import session_scope
from models.database import Employee, SalaryRecord
from utils.auth import require_permission, require_staff_permission
from utils.permissions import Permission
from utils.salary_access import (
    FULL_SALARY_ROLES,
    enforce_self_or_full_salary as _enforce_self_or_full_salary,
    resolve_salary_viewer_employee_id as _resolve_salary_viewer_employee_id,
)

router = APIRouter()


@router.get("/salaries/records")
def get_salary_records(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
):
    """查詢某月薪資記錄（支援分頁，預設一次最多 500 筆）"""
    # Lazy import 讓 test 對 _today_taipei 的 monkeypatch 仍可作用於本路徑。
    from . import _trigger_past_month_snapshot_if_missing

    _trigger_past_month_snapshot_if_missing(background_tasks)
    with session_scope() as session:
        viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)

        query = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .options(joinedload(Employee.job_title_rel))
            .filter(
                SalaryRecord.salary_year == year, SalaryRecord.salary_month == month
            )
        )
        if viewer_employee_id is not None:
            query = query.filter(SalaryRecord.employee_id == viewer_employee_id)
        records = query.order_by(Employee.name).offset(skip).limit(limit).all()

        results = []
        for record, emp in records:
            job_title = ""
            if emp.job_title_rel:
                job_title = emp.job_title_rel.name
            elif emp.title:
                job_title = emp.title

            results.append(
                {
                    "id": record.id,
                    "version": int(record.version or 1),
                    "employee_id": emp.id,
                    "employee_code": emp.employee_id,
                    "employee_name": emp.name,
                    "job_title": job_title,
                    "base_salary": record.base_salary,
                    "festival_bonus": record.festival_bonus,
                    "overtime_bonus": record.overtime_bonus,
                    "overtime_pay": record.overtime_pay,
                    "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                    "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                    "birthday_bonus": record.birthday_bonus or 0,
                    "performance_bonus": record.performance_bonus,
                    "special_bonus": record.special_bonus,
                    "supervisor_dividend": record.supervisor_dividend or 0,
                    "labor_insurance": record.labor_insurance_employee,
                    "health_insurance": record.health_insurance_employee,
                    "pension": record.pension_employee,
                    "late_deduction": record.late_deduction,
                    "early_leave_deduction": record.early_leave_deduction,
                    "missing_punch_deduction": record.missing_punch_deduction,
                    "absence_deduction": record.absence_deduction or 0,
                    "attendance_deduction": (record.late_deduction or 0)
                    + (record.early_leave_deduction or 0)
                    + (record.missing_punch_deduction or 0),
                    "leave_deduction": record.leave_deduction,
                    "other_deduction": record.other_deduction,
                    "gross_salary": record.gross_salary,
                    "total_deduction": record.total_deduction,
                    "net_salary": record.net_salary,
                    "is_finalized": record.is_finalized,
                    "finalized_at": (
                        record.finalized_at.isoformat() if record.finalized_at else None
                    ),
                    "finalized_by": record.finalized_by,
                    "remark": record.remark,
                    "calculated_at": (
                        record.updated_at.isoformat() if record.updated_at else None
                    ),
                    # 被 manual_adjust 寫過的欄位名單;前端可在欄位旁加上「人工調整」指示器,
                    # 提示該欄位不會被後續上游事件觸發的重算覆寫
                    "manual_overrides": list(record.manual_overrides or []),
                    # 前端 salaryResults 使用的欄位別名（供頁面重整後重建計算結果列表）
                    "pension_self": record.pension_employee or 0,
                    "total_deductions": record.total_deduction or 0,
                    "net_pay": record.net_salary or 0,
                }
            )

        return results


@router.get("/salaries/export-all")
def export_all_salaries(
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
    include_pending: bool = Query(
        False,
        description="是否包含未封存或待重算（needs_recalc）薪資；預設 False 只匯出可信任的封存資料。",
    ),
):
    """匯出全部員工薪資（xlsx 或 pdf）僅限 admin/hr。

    預設只匯出 is_finalized=True 且 needs_recalc=False 的薪資紀錄,避免會計把
    草稿/測試重算的薪資輸出成正式薪資表造成 A 錢空間。需要含草稿時必須帶
    include_pending=True,並寫入 explicit audit 留稽核軌跡。
    """
    from utils.audit import write_explicit_audit

    if current_user.get("role") not in FULL_SALARY_ROLES:
        raise HTTPException(
            status_code=403, detail="僅限系統管理員或人事主管匯出全員薪資"
        )
    with session_scope() as session:
        base_query = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(
                SalaryRecord.salary_year == year, SalaryRecord.salary_month == month
            )
        )
        if not include_pending:
            base_query = base_query.filter(
                SalaryRecord.is_finalized == True,  # noqa: E712
                SalaryRecord.needs_recalc == False,  # noqa: E712
            )
        records = base_query.order_by(Employee.name).all()

        if not records:
            detail = (
                "該月份無已封存且非待重算的薪資記錄；如需匯出草稿請加 include_pending=true"
                if not include_pending
                else "該月份無薪資記錄"
            )
            raise HTTPException(status_code=404, detail=detail)

        # 顯式稽核：GET 匯出不會經 AuditMiddleware；薪資匯出涉及全員實發/扣繳
        # 是高敏感度資料,必須留下「誰在何時匯出多少筆、是否含草稿」的軌跡。
        pending_count = sum(
            1 for rec, _ in records if (not rec.is_finalized) or rec.needs_recalc
        )
        write_explicit_audit(
            request,
            action="EXPORT",
            entity_type="salary",
            summary=(
                f"匯出全員薪資 {year}/{month:02d}（{format.upper()}，{len(records)} 筆"
                + (f"，含草稿/待重算 {pending_count} 筆" if include_pending else "")
                + "）"
            ),
            changes={
                "year": year,
                "month": month,
                "format": format,
                "include_pending": include_pending,
                "count": len(records),
                "pending_count": pending_count,
            },
        )

        if format == "pdf":
            from services.salary_slip import generate_salary_all_pdf

            pdf_bytes = generate_salary_all_pdf(records, year, month)
            filename = f"salary_all_{year}_{month:02d}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{filename}"
                },
            )

        from services.salary_slip import generate_salary_excel

        excel_bytes = generate_salary_excel(records, year, month)
        filename = f"salary_all_{year}_{month:02d}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )


@router.get("/salaries/history")
def get_salary_history(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    employee_id: int = Query(...),
    months: int = Query(12, ge=1, le=60),
):
    """查詢員工歷史薪資"""
    _enforce_self_or_full_salary(current_user, employee_id)
    with session_scope() as session:
        records = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.employee_id == employee_id)
            .order_by(SalaryRecord.salary_year.desc(), SalaryRecord.salary_month.desc())
            .limit(months)
            .all()
        )

        results = []
        for r in records:
            total_bonus = calculate_display_bonus_total(r)
            results.append(
                {
                    "id": r.id,
                    "year": r.salary_year,
                    "month": r.salary_month,
                    "base_salary": r.base_salary,
                    "total_bonus": total_bonus,
                    "labor_insurance": r.labor_insurance_employee,
                    "health_insurance": r.health_insurance_employee,
                    "attendance_deduction": (
                        (r.late_deduction or 0)
                        + (r.early_leave_deduction or 0)
                        + (r.missing_punch_deduction or 0)
                    ),
                    "leave_deduction": r.leave_deduction or 0,
                    "gross_salary": r.gross_salary,
                    "total_deduction": r.total_deduction,
                    "total_deductions": r.total_deduction,
                    "net_salary": r.net_salary,
                    "net_pay": r.net_salary,
                }
            )

        return results


@router.get("/salaries/history-all")
def get_salary_history_all(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """查詢全部員工年度薪資概覽（支援分頁，依員工分組）"""
    viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)
    with session_scope() as session:
        # 先取得符合條件的員工 id 清單（分頁依員工為單位）
        emp_subq = (
            session.query(Employee.id, Employee.name)
            .join(SalaryRecord, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.salary_year == year)
            .group_by(Employee.id, Employee.name)
            .order_by(Employee.name)
        )
        if viewer_employee_id is not None:
            emp_subq = emp_subq.filter(Employee.id == viewer_employee_id)
        total = emp_subq.count()
        emp_page = emp_subq.offset(skip).limit(limit).all()
        emp_ids = [e.id for e in emp_page]
        emp_name_map = {e.id: e.name for e in emp_page}

        if not emp_ids:
            return {"items": [], "total": total, "skip": skip, "limit": limit}

        records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.employee_id.in_(emp_ids),
            )
            .order_by(SalaryRecord.salary_month)
            .all()
        )

        grouped: dict = {eid: [] for eid in emp_ids}
        for r in records:
            grouped[r.employee_id].append(
                {
                    "month": r.salary_month,
                    "net_salary": r.net_salary,
                    "gross_salary": r.gross_salary,
                }
            )

        results = [
            {
                "employee_id": eid,
                "employee_name": emp_name_map[eid],
                "months": grouped[eid],
            }
            for eid in emp_ids
        ]
        return {"items": results, "total": total, "skip": skip, "limit": limit}
