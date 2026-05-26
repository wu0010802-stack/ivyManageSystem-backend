"""snapshot_leave step：算特休餘額 + daily_wage 折現，寫 JSONB snapshot。

依賴 utils.leave_quota_helpers.get_annual_leave_balance。
失敗條件：daily_wage 缺失或 0 → 422 LEAVE_BALANCE_NOT_FOUND（折現無法計算）。
"""

from datetime import datetime

from sqlalchemy.orm import Session

from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.orchestrator import OffboardingError, StepResult
from utils.leave_quota_helpers import get_annual_leave_balance
from utils.rounding import round_half_up


def _resolve_daily_wage(emp: Employee) -> float | None:
    """取員工日薪。先試直接欄位，否則 base_salary / 30。"""
    # 先試直接 daily_wage / daily_salary 欄位（若未來 migration 加上）
    daily = getattr(emp, "daily_wage", None) or getattr(emp, "daily_salary", None)
    if daily:
        return float(daily)
    # fallback：base_salary / 30（與 salary engine 對齊）
    monthly = getattr(emp, "monthly_salary", None) or getattr(emp, "base_salary", None)
    if monthly:
        return round_half_up(float(monthly) / 30.0, 2)
    return None


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    """計算特休餘額並以 daily_wage 折現，結果寫入 leave_balance_snapshot JSONB。"""
    emp = session.query(Employee).filter_by(id=record.employee_id).first()
    if emp is None:
        raise OffboardingError(
            f"員工 {record.employee_id} 不存在（snapshot_leave）",
            code="EMPLOYEE_NOT_FOUND",
        )

    daily_wage = _resolve_daily_wage(emp)
    if daily_wage is None or daily_wage == 0:
        raise OffboardingError(
            f"員工 {record.employee_id} 無 daily_wage / base_salary，無法折現特休",
            code="LEAVE_BALANCE_NOT_FOUND",
        )

    balance = get_annual_leave_balance(session, emp.id, record.resign_date)
    payout_amount = round_half_up(balance["remaining_days"] * daily_wage, 2)

    now = datetime.now()
    record.leave_balance_snapshot = {
        "snapshot_date": balance["snapshot_date"].isoformat(),
        "total_hours": balance["total_hours"],
        "used_hours": balance["used_hours"],
        "remaining_hours": balance["remaining_hours"],
        "remaining_days": balance["remaining_days"],
        "daily_wage": daily_wage,
        "payout_amount": payout_amount,
        "calc_rule_version": "labor_act_38_2026_v1",
    }
    record.leave_snapshot_at = now

    return {
        "step": "snapshot_leave",
        "status": "completed",
        "completed_at": now,
        "payload": {
            "days": balance["remaining_days"],
            "payout": payout_amount,
        },
        "error": None,
    }


def prefill_salary(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    """step 3 (prefill_leave_payout)：把 snapshot 結果寫入離職當月 SalaryRecord.unused_leave_payout。

    若離職當月 SalaryRecord 不存在則 SKIP（不新建；薪資 calculate 時會新建）；
    存在則：
    1. 覆寫 unused_leave_payout 並標 stale
    2. 寫一筆 UnusedLeavePayoutLog source_type='offboarding' 留證據鏈
    3. Revoke 該員工 active OvertimeCompLeaveGrant，避免 scheduler 再撈
    """
    from decimal import Decimal

    from api.employees import _mark_employee_salary_stale
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.salary import SalaryRecord
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    snap = record.leave_balance_snapshot
    if not snap:
        return {
            "step": "prefill_leave_payout",
            "status": "skipped",
            "completed_at": datetime.now(),
            "payload": {"reason": "no_snapshot"},
            "error": None,
        }

    target = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == record.employee_id,
            SalaryRecord.salary_year == record.resign_date.year,
            SalaryRecord.salary_month == record.resign_date.month,
        )
        .first()
    )
    if target is None:
        return {
            "step": "prefill_leave_payout",
            "status": "skipped",
            "completed_at": datetime.now(),
            "payload": {"reason": "salary_record_not_yet_created"},
            "error": None,
        }

    target.unused_leave_payout = snap["payout_amount"]
    _mark_employee_salary_stale(session, record.employee_id)

    # ── 寫 UnusedLeavePayoutLog 留證據鏈 ──
    daily_wage = float(snap.get("daily_wage", 0))
    hourly_wage = Decimal(str(round(daily_wage / 8, 2)))
    log = UnusedLeavePayoutLog(
        employee_id=record.employee_id,
        source_type="offboarding",
        source_ref_id=record.employee_id,
        hours=float(snap.get("remaining_hours", 0)),
        hourly_wage=hourly_wage,
        amount=Decimal(str(snap.get("payout_amount", 0))),
        wage_basis_date=record.resign_date,
        salary_record_id=target.id,
        salary_period_year=target.salary_year,
        salary_period_month=target.salary_month,
        meta={
            "offboarding_record_id": record.employee_id,
            "termination_date": record.resign_date.isoformat(),
            "snapshot_remaining_days": snap.get("remaining_days"),
        },
    )
    session.add(log)

    # ── Revoke active grants 防 scheduler 重複結算 ──
    active_grants = (
        session.query(OvertimeCompLeaveGrant)
        .filter_by(employee_id=record.employee_id, status="active")
        .all()
    )
    for g in active_grants:
        g.status = "revoked"
    session.flush()

    return {
        "step": "prefill_leave_payout",
        "status": "completed",
        "completed_at": datetime.now(),
        "payload": {
            "salary_record_id": target.id,
            "amount": snap["payout_amount"],
        },
        "error": None,
    }
