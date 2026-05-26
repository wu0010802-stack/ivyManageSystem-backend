"""
Portal – leave quota expiry preview endpoint

GET /portal/me/leave-quota-expiry

回傳員工補休結餘、最早到期 grant、下個週年日、預計結算月，
供前端 widget 顯示提醒資訊。
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_session
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.employee import Employee
from utils.auth import get_current_user
from utils.taipei_time import today_taipei
from services.leave_quota_expiry.helpers import (
    _compensatory_balance,
    _next_month,
)
from ._shared import _get_employee

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers (module-level so tests can patch them)
# ---------------------------------------------------------------------------


def _get_earliest_active_grant(
    employee_id: int, session: Session
) -> Optional[OvertimeCompLeaveGrant]:
    """撈 employee 最早到期的 active grant；無 active grant 回 None。"""
    return (
        session.query(OvertimeCompLeaveGrant)
        .filter(
            OvertimeCompLeaveGrant.employee_id == employee_id,
            OvertimeCompLeaveGrant.status == "active",
        )
        .order_by(OvertimeCompLeaveGrant.expires_at.asc())
        .first()
    )


def _compute_next_anniversary(hire_date: Optional[date], today: date) -> Optional[date]:
    """計算員工下一個 hire_date 週年。

    - hire_date is None → None
    - 2/29 + N 年落非閏年 → 2/28（與 _add_one_year_with_feb29_handling 邏輯一致）
    """
    if hire_date is None:
        return None

    years = today.year - hire_date.year
    if (today.month, today.day) >= (hire_date.month, hire_date.day):
        years += 1

    target_year = hire_date.year + years
    try:
        return hire_date.replace(year=target_year)
    except ValueError:
        # 2/29 落非閏年 → 2/28
        return hire_date.replace(year=target_year, day=28)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/me/leave-quota-expiry")
def get_my_leave_quota_expiry(
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """員工自助入口：補休結餘 + 最早到期 grant + 下個週年 + 預計結算月。

    用途：前端顯示「您有 N 小時補休即將於 YYYY-MM-DD 到期，
    預計於 YYYY-MM 結算月兌現」之提醒 widget。

    Response 200:
    {
        "compensatory_balance": float,
        "earliest_expiring_grant": {
            "expires_at": "YYYY-MM-DD",
            "unexpired_hours": float,
        } | null,
        "next_anniversary": "YYYY-MM-DD" | null,
        "expected_payout_month": "YYYY-MM" | null,
    }
    """
    emp = _get_employee(session, current_user)
    emp_id: int = emp.id

    # 1. 補休結餘
    balance = _compensatory_balance(emp_id, session)

    # 2. 最早到期 active grant
    earliest = _get_earliest_active_grant(emp_id, session)
    earliest_dict = None
    if earliest is not None:
        earliest_dict = {
            "expires_at": earliest.expires_at.isoformat(),
            "unexpired_hours": float(earliest.granted_hours - earliest.consumed_hours),
        }

    # 3. 下個週年日
    today = today_taipei()
    next_anniv = _compute_next_anniversary(emp.hire_date, today)

    # 4. 預計結算月：取最近的候選日（grant expires_at 或 next_anniversary）
    candidates: list[date] = []
    if earliest is not None:
        candidates.append(earliest.expires_at)
    if next_anniv is not None:
        candidates.append(next_anniv)

    expected_payout_month: Optional[str] = None
    if candidates:
        closest = min(candidates)
        py, pm = _next_month(closest)
        expected_payout_month = f"{py}-{pm:02d}"

    return {
        "compensatory_balance": balance,
        "earliest_expiring_grant": earliest_dict,
        "next_anniversary": next_anniv.isoformat() if next_anniv else None,
        "expected_payout_month": expected_payout_month,
    }
