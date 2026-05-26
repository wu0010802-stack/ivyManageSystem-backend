"""HR 補休 / 特休到期管理 API。

4 個端點供 HR 查詢即將到期 grant、即將週年員工、結算歷史，以及手動 trigger scheduler。
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query

from models.base import get_session
from models.employee import Employee
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter(prefix="/leave-quota-expiry", tags=["leave-quota-expiry"])


@router.get("/upcoming")
def list_upcoming_expiring_grants(
    days: int = Query(30, ge=1, le=365),
    _: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
):
    """列出未來 N 天內到期的 active 補休 grant。"""
    today = date.today()
    end = today + timedelta(days=days)
    session = get_session()
    try:
        grants = (
            session.query(OvertimeCompLeaveGrant)
            .filter(
                OvertimeCompLeaveGrant.status == "active",
                OvertimeCompLeaveGrant.expires_at >= today,
                OvertimeCompLeaveGrant.expires_at <= end,
            )
            .order_by(OvertimeCompLeaveGrant.expires_at.asc())
            .all()
        )
        return {
            "grants": [
                {
                    "grant_id": g.id,
                    "employee_id": g.employee_id,
                    "granted_hours": g.granted_hours,
                    "consumed_hours": g.consumed_hours,
                    "unexpired_hours": g.granted_hours - g.consumed_hours,
                    "granted_at": g.granted_at.isoformat(),
                    "expires_at": g.expires_at.isoformat(),
                }
                for g in grants
            ]
        }
    finally:
        session.close()


@router.get("/anniversaries")
def list_upcoming_anniversaries(
    days: int = Query(30, ge=1, le=365),
    _: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
):
    """列出未來 N 天內滿週年（特休週年 cutover）的在職員工。

    使用 Python-side 過濾：撈所有在職員工後逐一比對，員工數 ≤200 時性能 OK。
    只列入職已滿 6 個月（180 天）以上的員工，過濾掉試用期尚短者。
    """
    today = date.today()
    session = get_session()
    try:
        emps = session.query(Employee).filter(Employee.is_active.is_(True)).all()
        results = []
        for emp in emps:
            if emp.hire_date is None:
                continue
            # 只考慮入職滿 6 個月的員工（至少有一次週年可計算）
            if emp.hire_date > today - timedelta(days=180):
                continue
            for offset in range(days + 1):
                check = today + timedelta(days=offset)
                try:
                    anniv = emp.hire_date.replace(year=check.year)
                except ValueError:
                    # 2/29 非閏年 fallback 2/28
                    anniv = emp.hire_date.replace(year=check.year, day=28)
                if anniv == check:
                    results.append(
                        {
                            "employee_id": emp.id,
                            "hire_date": emp.hire_date.isoformat(),
                            "next_anniversary": anniv.isoformat(),
                        }
                    )
                    break
        return {"anniversaries": results}
    finally:
        session.close()


@router.get("/payout-history")
def list_payout_history(
    limit: int = Query(50, ge=1, le=500),
    _: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """列出 unused_leave_payout_log 結算歷史（最新在前）。"""
    session = get_session()
    try:
        logs = (
            session.query(UnusedLeavePayoutLog)
            .order_by(UnusedLeavePayoutLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "logs": [
                {
                    "log_id": log.id,
                    "employee_id": log.employee_id,
                    "source_type": log.source_type,
                    "hours": log.hours,
                    "amount": float(log.amount),
                    "wage_basis_date": log.wage_basis_date.isoformat(),
                    "salary_period": f"{log.salary_period_year}-{log.salary_period_month:02d}",
                    "salary_record_id": log.salary_record_id,
                    "meta": log.meta,
                }
                for log in logs
            ]
        }
    finally:
        session.close()


@router.post("/run-now")
def run_scheduler_now(
    _: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """手動 trigger 補休到期 + 特休週年 cutover scheduler（idempotent 重跑安全）。

    與 asyncio scheduler 呼叫相同的 service 函式，savepoint 隔離單員工失敗。
    """
    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    today = date.today()
    session = get_session()
    try:
        comp_summary = expire_comp_leave_grants(today, session)
        cutover_summary = cutover_annual_leave_anniversaries(today, session)
        session.commit()
        return {"comp_summary": comp_summary, "cutover_summary": cutover_summary}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
