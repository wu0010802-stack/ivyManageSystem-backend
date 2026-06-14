"""
教師 portal 補休歷史明細：grant ledger + payout log。

GET /portal/me/comp-leave-grants  — 員工自己的 grant 全狀態列表（granted_at DESC）
GET /portal/me/payout-history     — 員工自己的 unused_leave_payout_log（created_at DESC）
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from utils.auth import get_current_user

from ._shared import _get_employee

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal query helpers（module-level so tests can patch them）
# ---------------------------------------------------------------------------


def _query_grants(session: Session, employee_id: int) -> list:
    """撈 employee 全部 grant，granted_at DESC。"""
    return (
        session.query(OvertimeCompLeaveGrant)
        .filter(OvertimeCompLeaveGrant.employee_id == employee_id)
        .order_by(OvertimeCompLeaveGrant.granted_at.desc())
        .all()
    )


def _query_payout_logs(session: Session, employee_id: int) -> list:
    """撈 employee 全部 payout log，created_at DESC。"""
    return (
        session.query(UnusedLeavePayoutLog)
        .filter(UnusedLeavePayoutLog.employee_id == employee_id)
        .order_by(UnusedLeavePayoutLog.created_at.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/me/comp-leave-grants")
def list_my_comp_leave_grants(
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
):
    """員工自助入口：補休 grant ledger 全狀態明細（active / expired / revoked）。

    Response 200:
    {
        "grants": [
            {
                "grant_id": int,
                "granted_hours": float,
                "consumed_hours": float,
                "remaining_hours": float,
                "granted_at": "YYYY-MM-DD",
                "expires_at": "YYYY-MM-DD",
                "status": str,
                "expired_at": "YYYY-MM-DDTHH:MM:SS" | null,
            },
            ...
        ]
    }

    Order: granted_at DESC（最近的在前）
    """
    emp = _get_employee(session, current_user)
    grants = _query_grants(session, emp.id)

    return {
        "grants": [
            {
                "grant_id": g.id,
                "granted_hours": g.granted_hours,
                "consumed_hours": g.consumed_hours,
                "remaining_hours": g.granted_hours - g.consumed_hours,
                "granted_at": g.granted_at.isoformat(),
                "expires_at": g.expires_at.isoformat(),
                "status": g.status,
                "expired_at": (
                    g.expired_at.isoformat() if g.expired_at is not None else None
                ),
            }
            for g in grants
        ]
    }


@router.get("/me/payout-history")
def list_my_payout_history(
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
):
    """員工自助入口：未休假折算工資兌現紀錄（含 source_type）。

    Response 200:
    {
        "logs": [
            {
                "log_id": int,
                "source_type": str,        # comp_grant_expiry / annual_anniversary / offboarding
                "hours": float,
                "hourly_wage": float,
                "amount": float,
                "wage_basis_date": "YYYY-MM-DD",
                "salary_period": "YYYY-MM",
                "meta": dict,
            },
            ...
        ]
    }

    Order: created_at DESC（最新在前）
    """
    emp = _get_employee(session, current_user)
    logs = _query_payout_logs(session, emp.id)

    return {
        "logs": [
            {
                "log_id": log.id,
                "source_type": log.source_type,
                "hours": log.hours,
                "hourly_wage": float(log.hourly_wage),
                "amount": float(log.amount),
                "wage_basis_date": log.wage_basis_date.isoformat(),
                "salary_period": f"{log.salary_period_year}-{log.salary_period_month:02d}",
                "meta": log.meta if log.meta is not None else {},
            }
            for log in logs
        ]
    }
