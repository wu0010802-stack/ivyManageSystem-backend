"""Pure function helpers — 不依賴 session，可獨立測試。"""

from calendar import isleap
from datetime import date
from typing import Optional

from sqlalchemy import and_, extract, func, or_
from sqlalchemy.orm import Session


def _next_month(today: date) -> tuple[int, int]:
    """跨年 12→1 wrap。

    Returns:
        (year, month) tuple
    """
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


def _add_one_year_with_feb29_handling(d: date) -> date:
    """2/29 + 1y 落非閏年順延 2/28。

    When adding one year to 2/29 of a leap year and the target year is not
    a leap year, returns 2/28 instead of raising ValueError.
    """
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        # Handles 2/29 → non-leap year case
        return d.replace(year=d.year + 1, day=28)


def _resolve_hourly_wage(emp, ref_date: date) -> float:
    """月薪/30/8 或 hourly_rate。

    For hourly employees, returns hourly_rate.
    For monthly employees, returns base_salary / 30 / 8.

    Args:
        emp: Employee object with employee_type and either hourly_rate or base_salary
        ref_date: Reference date (future-proof for EmployeeSalaryHistory lookup)

    Returns:
        Hourly wage as float. Returns 0.0 if employee_type is unknown or salary is ≤ 0.

    註：未來若引入 EmployeeSalaryHistory 取 ref_date 當下生效薪資，
    在此 helper 內展開即可，scheduler caller 不需改。
    """
    if emp.employee_type == "hourly":
        return float(emp.hourly_rate or 0)

    monthly = float(emp.base_salary or 0)
    if monthly <= 0:
        return 0.0

    return monthly / 30 / 8


# ──────────────────────────────────────────────────────────────────────────────
# SQL helpers
# ──────────────────────────────────────────────────────────────────────────────


def _is_anniversary_today_sql(hire_date_col, today: date):
    """SQL 表達式：員工 hire_date 月日 == today 月日。

    2/29 fallback：非閏年的 2/28 同時撈 hire_date=2/29 員工。
    """
    base = and_(
        extract("month", hire_date_col) == today.month,
        extract("day", hire_date_col) == today.day,
    )
    if today.month == 2 and today.day == 28 and not isleap(today.year):
        return or_(
            base,
            and_(
                extract("month", hire_date_col) == 2,
                extract("day", hire_date_col) == 29,
            ),
        )
    return base


def _approved_annual_used_in_period(
    employee_id: int, period_start: date, period_end: date, session: Session
) -> float:
    """加總期間內已核准的 annual leave 時數。"""
    from models.leave import LeaveRecord

    used = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.is_approved.is_(True),
            LeaveRecord.start_date >= period_start,
            LeaveRecord.start_date < period_end,
        )
        .scalar()
    )
    return float(used or 0)


def _find_or_none_salary_record(
    employee_id: int, year: int, month: int, session: Session
):
    """撈該員工該月 SalaryRecord；不存在返 None。"""
    from models.salary import SalaryRecord

    return (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .first()
    )


def _compensatory_balance(employee_id: int, session: Session) -> float:
    """員工目前可用補休餘額 = SUM(granted_hours - consumed_hours) WHERE status='active'

    此為新的 source of truth；既有 LeaveQuota.compensatory.total_hours 降級為快取。
    """
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    balance = (
        session.query(
            func.coalesce(
                func.sum(
                    OvertimeCompLeaveGrant.granted_hours
                    - OvertimeCompLeaveGrant.consumed_hours
                ),
                0,
            )
        )
        .filter(
            OvertimeCompLeaveGrant.employee_id == employee_id,
            OvertimeCompLeaveGrant.status == "active",
        )
        .scalar()
    )
    return float(balance or 0)
