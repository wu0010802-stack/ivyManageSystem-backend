"""特休（annual leave）餘額計算 public helper。

從 api/leaves_quota.py:_get_used_hours 抽出供 services/offboarding/ 與其他模組共用。
公式：remaining_hours = quota.total_hours - approved_used_hours
（不含 pending；離職時 pending 應由 admin 先處理）
"""

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.leave import LeaveQuota, LeaveRecord


def get_annual_leave_balance(
    session: Session,
    employee_id: int,
    snapshot_date: date,
) -> dict:
    """回傳指定員工於 snapshot_date 當日特休餘額。

    school_year row 優先於 legacy（school_year=None）row。
    只計入 is_approved=True 的記錄，排除 pending（None）與 rejected（False）。

    Args:
        session: SQLAlchemy Session
        employee_id: 員工 PK
        snapshot_date: 快照日期（決定適用年度 = snapshot_date.year）

    Returns:
        dict with keys:
        - total_hours: float       quota.total_hours（school_year row 優先）
        - used_hours: float        已 approved 的 annual hours（年度內 start_date <= snapshot_date）
        - remaining_hours: float   = total - used，下限 0
        - remaining_days: float    = remaining_hours / 8，四捨五入 2 位
        - snapshot_date: date      傳入值，回傳供 audit
    """
    year = snapshot_date.year

    # school_year row 優先（民國學年 IS NOT NULL）
    quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.year == year,
            LeaveQuota.leave_type == "annual",
            LeaveQuota.school_year.isnot(None),
        )
        .first()
    )
    if quota is None:
        # fallback: legacy row（school_year IS NULL）
        quota = (
            session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == year,
                LeaveQuota.leave_type == "annual",
                LeaveQuota.school_year.is_(None),
            )
            .first()
        )

    total_hours = float(quota.total_hours) if quota else 0.0

    used_hours = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0.0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.is_approved
            == True,  # noqa: E712 — SQLAlchemy requires == not `is`
            LeaveRecord.start_date >= date(year, 1, 1),
            LeaveRecord.start_date <= snapshot_date,
        )
        .scalar()
    ) or 0.0
    used_hours = float(used_hours)

    remaining_hours = max(0.0, total_hours - used_hours)
    remaining_days = round(remaining_hours / 8.0, 2)

    return {
        "total_hours": total_hours,
        "used_hours": used_hours,
        "remaining_hours": remaining_hours,
        "remaining_days": remaining_days,
        "snapshot_date": snapshot_date,
    }
