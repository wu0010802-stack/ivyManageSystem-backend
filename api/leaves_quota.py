"""
Leave quota management: constants, helpers, and quota routes.

This module is split out from leaves.py to keep each file focused.
It owns all quota-related constants, DB query helpers, limit checks,
and the three quota CRUD endpoints.
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func

from models.database import get_session, Employee, LeaveRecord, LeaveQuota
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

# ============ Constants ============

# 請假扣薪規則（依勞基法）
LEAVE_DEDUCTION_RULES: dict[str, float] = {
    "personal": 1.0,          # 事假: 全扣
    "sick": 0.5,               # 病假: 扣半薪
    "menstrual": 0.5,          # 生理假: 扣半薪
    "annual": 0.0,             # 特休: 不扣
    "maternity": 0.0,          # 產假: 不扣
    "paternity": 0.0,          # 陪產假: 不扣（舊）
    "official": 0.0,           # 公假: 不扣
    "marriage": 0.0,           # 婚假: 不扣
    "bereavement": 0.0,        # 喪假: 不扣
    "prenatal": 0.0,           # 產檢假: 不扣
    "paternity_new": 0.0,      # 陪產檢及陪產假: 不扣
    "miscarriage": 0.0,        # 流產假: 不扣
    "family_care": 1.0,        # 家庭照顧假: 不給薪（併入事假）
    "parental_unpaid": 0.0,    # 育嬰留職停薪: 不扣（留停期間無薪）
    "compensatory": 0.0,       # 補休：不扣薪
}

LEAVE_TYPE_LABELS: dict[str, str] = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
    "compensatory": "補休",
}

# 有年度上限的假別（其餘為事件型，不追蹤）
QUOTA_LEAVE_TYPES: set[str] = {"annual", "sick", "menstrual", "personal", "family_care"}

# 法定年度配額（小時），不含特休（特休依年資動態計算）
STATUTORY_QUOTA_HOURS: dict[str, float] = {
    "sick":        240.0,   # 病假 30天
    "menstrual":    96.0,   # 生理假 12天
    "personal":    112.0,   # 事假 14天
    "family_care":  56.0,   # 家庭照顧假 7天（勞基法第20條）
}

# 法定年度累計上限（小時）— 事件型假別（不在 QUOTA_LEAVE_TYPES 中）
ANNUAL_MAX_HOURS: dict[str, float] = {
    "marriage": 64.0,  # 婚假 8天（勞工請假規則第3條）
}

# 法定單次申請上限（小時）
SINGLE_REQUEST_MAX_HOURS: dict[str, float] = {
    "bereavement": 64.0,  # 喪假最高 8天（依親等實際上限 3–8天）
}

# 法定每月上限（小時）
MONTHLY_MAX_HOURS: dict[str, float] = {
    "menstrual": 8.0,  # 生理假每月 1天（勞基法第14-1條）
}


# ============ Helpers ============

def _calc_annual_leave_hours(hire_date: "date | None", year: int) -> float:
    """依勞基法第38條計算特休配額時數，以 year 年 12/31 為基準日計算年資"""
    if hire_date is None:
        return 0.0
    ref = date(year, 12, 31)
    if hire_date > ref:
        return 0.0

    # 完整月數（不足月不計）
    months = (ref.year - hire_date.year) * 12 + (ref.month - hire_date.month)
    if ref.day < hire_date.day:
        months -= 1

    if months < 6:
        return 0.0
    elif months < 12:
        return 24.0    # 3天
    complete_years = months // 12
    if complete_years < 2:
        return 56.0    # 7天
    elif complete_years < 3:
        return 80.0    # 10天
    elif complete_years < 5:
        return 112.0   # 14天
    elif complete_years < 10:
        return 120.0   # 15天
    else:
        days = min(15 + complete_years - 10, 30)
        return float(days * 8)


def _get_used_hours(session, employee_id: int, year: int, leave_type: str) -> float:
    """查詢該年度已核准的請假時數"""
    result = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved == True,
        func.extract('year', LeaveRecord.start_date) == year,
    ).scalar()
    return float(result)


def _get_pending_hours(session, employee_id: int, year: int, leave_type: str) -> float:
    """查詢該年度待審核的請假時數"""
    result = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved.is_(None),
        func.extract('year', LeaveRecord.start_date) == year,
    ).scalar()
    return float(result)


def _get_approved_hours_in_year(
    session, employee_id: int, year: int, leave_type: str, exclude_id: int = None
) -> float:
    """查詢年度已核准時數（可排除指定記錄，供更新時使用）"""
    q = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved == True,
        func.extract('year', LeaveRecord.start_date) == year,
    )
    if exclude_id:
        q = q.filter(LeaveRecord.id != exclude_id)
    return float(q.scalar())


def _get_pending_hours_in_year(
    session, employee_id: int, year: int, leave_type: str, exclude_id: int = None
) -> float:
    """查詢年度待審核時數（可排除指定記錄）"""
    q = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved.is_(None),
        func.extract('year', LeaveRecord.start_date) == year,
    )
    if exclude_id:
        q = q.filter(LeaveRecord.id != exclude_id)
    return float(q.scalar())


def _get_approved_hours_in_month(
    session, employee_id: int, year: int, month: int,
    leave_type: str, exclude_id: int = None
) -> float:
    """查詢指定月份已核准時數（可排除指定記錄）"""
    q = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved == True,
        func.extract('year', LeaveRecord.start_date) == year,
        func.extract('month', LeaveRecord.start_date) == month,
    )
    if exclude_id:
        q = q.filter(LeaveRecord.id != exclude_id)
    return float(q.scalar())


def _get_pending_hours_in_month(
    session, employee_id: int, year: int, month: int,
    leave_type: str, exclude_id: int = None
) -> float:
    """查詢指定月份待審核時數（可排除指定記錄）"""
    q = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved.is_(None),
        func.extract('year', LeaveRecord.start_date) == year,
        func.extract('month', LeaveRecord.start_date) == month,
    )
    if exclude_id:
        q = q.filter(LeaveRecord.id != exclude_id)
    return float(q.scalar())


def _quota_row(session, quota: "LeaveQuota", year: int) -> dict:
    used = _get_used_hours(session, quota.employee_id, year, quota.leave_type)
    pending = _get_pending_hours(session, quota.employee_id, year, quota.leave_type)
    remaining = max(0.0, quota.total_hours - used)
    return {
        "id": quota.id,
        "employee_id": quota.employee_id,
        "year": quota.year,
        "leave_type": quota.leave_type,
        "leave_type_label": LEAVE_TYPE_LABELS.get(quota.leave_type, quota.leave_type),
        "total_hours": quota.total_hours,
        "used_hours": used,
        "pending_hours": pending,
        "remaining_hours": remaining,
        "note": quota.note,
    }


def _check_leave_limits(
    session, employee_id: int, leave_type: str,
    start_date: date, leave_hours: float, exclude_id: int = None,
    include_pending: bool = True,
) -> None:
    """
    針對有法定上限的假別進行驗證，超限時 raise HTTPException(400)。

    婚假：全年累計 ≤ 8天（64小時）
    喪假：單次申請 ≤ 8天（64小時，實際依親等）
    生理假：當月累計 ≤ 1天（8小時）

    include_pending=True（預設，用於新增/編輯）：已核准 + 待審合計納入
    include_pending=False（用於核准動作）：僅計算已核准時數
    """
    # ── 婚假：年累計上限 ──────────────────────────────────
    if leave_type in ANNUAL_MAX_HOURS:
        max_h = ANNUAL_MAX_HOURS[leave_type]
        approved = _get_approved_hours_in_year(
            session, employee_id, start_date.year, leave_type, exclude_id
        )
        if include_pending:
            pending = _get_pending_hours_in_year(
                session, employee_id, start_date.year, leave_type, exclude_id
            )
            committed = approved + pending
        else:
            pending = 0.0
            committed = approved
        if committed + leave_hours > max_h:
            remaining = max(0.0, max_h - committed)
            pending_note = f"、待審 {pending / 8:.1f} 天" if include_pending and pending > 0 else ""
            raise HTTPException(
                status_code=400,
                detail=(
                    f"婚假全年上限 8 天（{max_h:.0f} 小時），"
                    f"本年已核准 {approved / 8:.1f} 天{pending_note}，"
                    f"剩餘 {remaining / 8:.1f} 天（{remaining:.0f} 小時）"
                ),
            )

    # ── 喪假：單次申請上限 ────────────────────────────────
    if leave_type in SINGLE_REQUEST_MAX_HOURS:
        max_h = SINGLE_REQUEST_MAX_HOURS[leave_type]
        if leave_hours > max_h:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"喪假單次申請不得超過 8 天（{max_h:.0f} 小時）。"
                    "實際上限依親等：父母/配偶/子女 8 天、祖父母等 6 天、兄弟姐妹等 3 天"
                ),
            )

    # ── 生理假：每月上限 ──────────────────────────────────
    if leave_type in MONTHLY_MAX_HOURS:
        max_h = MONTHLY_MAX_HOURS[leave_type]
        year, month = start_date.year, start_date.month
        approved = _get_approved_hours_in_month(
            session, employee_id, year, month, leave_type, exclude_id
        )
        if include_pending:
            pending_m = _get_pending_hours_in_month(
                session, employee_id, year, month, leave_type, exclude_id
            )
            committed = approved + pending_m
        else:
            pending_m = 0.0
            committed = approved
        if committed + leave_hours > max_h:
            remaining = max(0.0, max_h - committed)
            pending_note = f"、待審 {pending_m:.0f} 小時" if include_pending and pending_m > 0 else ""
            raise HTTPException(
                status_code=400,
                detail=(
                    f"生理假每月上限 1 天（{max_h:.0f} 小時），"
                    f"{year}/{month:02d} 月已核准 {approved:.0f} 小時{pending_note}，"
                    f"剩餘 {remaining:.0f} 小時"
                ),
            )


def _check_quota(
    session, employee_id: int, leave_type: str,
    year: int, leave_hours: float, exclude_id: int = None,
    include_pending: bool = True,
) -> None:
    """
    針對有年度配額的假別（QUOTA_LEAVE_TYPES）檢查剩餘配額，
    超過時 raise HTTPException(400)。

    include_pending=True（預設，用於新增/編輯送出）：
      已核准 + 待審時數合計納入上限計算，防止員工同時大量送出假單
      規避配額（concurrent flooding 攻擊）。
    include_pending=False（用於核准動作）：
      僅計算已核准時數，確保主管核准時也不超出年度配額。
    若該員工尚未初始化配額（LeaveQuota 無記錄）則略過，不強制攔截。
    """
    if leave_type not in QUOTA_LEAVE_TYPES:
        return

    quota = session.query(LeaveQuota).filter(
        LeaveQuota.employee_id == employee_id,
        LeaveQuota.year == year,
        LeaveQuota.leave_type == leave_type,
    ).first()

    if quota is None:
        return  # 配額未初始化，略過檢查

    approved = _get_approved_hours_in_year(
        session, employee_id, year, leave_type, exclude_id
    )
    if include_pending:
        pending = _get_pending_hours_in_year(
            session, employee_id, year, leave_type, exclude_id
        )
        committed = approved + pending
    else:
        pending = 0.0
        committed = approved

    remaining = max(0.0, quota.total_hours - committed)

    if leave_hours > remaining:
        label = LEAVE_TYPE_LABELS.get(leave_type, leave_type)
        pending_note = f"、待審 {pending:.0f} 小時" if include_pending and pending > 0 else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label}年度配額 {quota.total_hours:.0f} 小時"
                f"（{quota.total_hours / 8:.1f} 天），"
                f"已核准 {approved:.0f} 小時{pending_note}，"
                f"剩餘可用 {remaining:.0f} 小時（{remaining / 8:.1f} 天），"
                f"本次申請 {leave_hours:.1f} 小時超過剩餘配額"
            ),
        )


# ============ Pydantic Models ============

class QuotaUpdate(BaseModel):
    total_hours: float = Field(..., ge=0)
    note: Optional[str] = None


# ============ Router ============

quota_router = APIRouter()


@quota_router.get("/leaves/quotas")
def get_leave_quotas(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    leave_type: Optional[str] = None,
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """查詢請假配額，含動態計算已使用、待審、剩餘時數"""
    if year is None:
        year = date.today().year
    session = get_session()
    try:
        q = session.query(LeaveQuota).filter(LeaveQuota.year == year)
        if employee_id:
            q = q.filter(LeaveQuota.employee_id == employee_id)
        if leave_type:
            q = q.filter(LeaveQuota.leave_type == leave_type)
        quotas = q.order_by(LeaveQuota.employee_id, LeaveQuota.leave_type).all()
        return [_quota_row(session, quota, year) for quota in quotas]
    finally:
        session.close()


@quota_router.post("/leaves/quotas/init", status_code=200)
def init_leave_quotas(
    employee_id: int,
    year: Optional[int] = None,
    current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE)),
):
    """
    依勞基法自動初始化（或重新計算）指定員工的年度配額。
    - 特休：依 hire_date 年資計算
    - 其他有上限假別：使用法定固定時數
    - 已存在的配額只更新 total_hours 與 note，不刪除手動調整記錄
    """
    if year is None:
        year = date.today().year
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        upserted = []
        for lt in QUOTA_LEAVE_TYPES:
            if lt == "annual":
                hours = _calc_annual_leave_hours(emp.hire_date, year)
                if emp.hire_date:
                    ref = date(year, 12, 31)
                    months = (ref.year - emp.hire_date.year) * 12 + (ref.month - emp.hire_date.month)
                    if ref.day < emp.hire_date.day:
                        months -= 1
                    note = f"年資 {months // 12} 年 {months % 12} 個月（依勞基法第38條）"
                else:
                    note = "到職日未設定，配額為 0"
            else:
                hours = STATUTORY_QUOTA_HOURS[lt]
                note = "法定年度上限"

            quota = session.query(LeaveQuota).filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == year,
                LeaveQuota.leave_type == lt,
            ).first()
            if quota:
                quota.total_hours = hours
                quota.note = note
            else:
                quota = LeaveQuota(
                    employee_id=employee_id,
                    year=year,
                    leave_type=lt,
                    total_hours=hours,
                    note=note,
                )
                session.add(quota)
            upserted.append(lt)

        session.commit()
        logger.info("初始化員工 %s（ID:%d）%d 年度假別配額：%s", emp.name, employee_id, year, upserted)

        # 重新查詢後回傳
        quotas = session.query(LeaveQuota).filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.year == year,
        ).all()
        return [_quota_row(session, q, year) for q in quotas]
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@quota_router.put("/leaves/quotas/{quota_id}")
def update_leave_quota(
    quota_id: int,
    data: QuotaUpdate,
    current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE)),
):
    """手動調整配額（例如主管核准額外特休）"""
    if data.total_hours < 0:
        raise HTTPException(status_code=400, detail="配額時數不得為負數")
    session = get_session()
    try:
        quota = session.query(LeaveQuota).filter(LeaveQuota.id == quota_id).first()
        if not quota:
            raise HTTPException(status_code=404, detail="配額記錄不存在")
        quota.total_hours = data.total_hours
        if data.note is not None:
            quota.note = data.note
        session.commit()
        return _quota_row(session, quota, quota.year)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
