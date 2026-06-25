"""補休 grant 到期結算：scheduler 主邏輯之一。

每日由 asyncio polling scheduler 呼叫 `expire_comp_leave_grants(today, session)`，
撈出所有 `expires_at <= today` 且 `status='active'` 的 grant，
依員工聚合後：

1. 計算未消耗時數 × 時薪 → amount（HALF_UP 進位）
2. 寫 `UnusedLeavePayoutLog`（source_type='comp_grant_expiry'）
3. Layer 1：若目標月 SalaryRecord 存在且未 finalize，直寫 `unused_leave_payout += amount`
   並反向綁定 `log.salary_record_id`
4. Layer 2：SR 不存在或已 finalize → `log.salary_record_id=None`，由 salary engine
   calculate 時撈 pending log 寫入
5. 更新所有 grant：`status='expired'`, `expired_at`, `payout_log_id`, `payout_salary_record_id`

全用完 (unexpired_hours == 0) 的 grant 仍 mark expired，但不建 log。
已離職員工（`is_active=False`）由 offboarding path 處理，此處跳過。
每個員工用 savepoint（`session.begin_nested()`）隔離，單員工失敗不影響其他人。
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

_TAIPEI_TZ = ZoneInfo("Asia/Taipei")

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.employee import Employee
from models.leave import LeaveQuota, LeaveRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from services.leave_quota_expiry.helpers import (
    _find_or_none_salary_record,
    _next_month,
    _resolve_hourly_wage,
)
from services.salary.unused_leave_pay import calculate_unused_leave_compensation
from utils.academic import resolve_current_academic_term
from utils.rounding import round_half_up


def _decrement_comp_quota(session, emp_id: int, target_date, hours: float) -> None:
    """到期折現後同步扣減補休 LeaveQuota.total_hours（rank 11）。

    配額檢查 remaining = total_hours − 已核准/待審補休；到期折現把未消耗時數換成現金
    後，那些時數已不可再當補休請，total_hours 須同步扣除，否則檢查見幽靈額度（檢查
    放行、消耗 FIFO 找不到 active grant → 422）。列解析與發放/檢查對齊（學年優先、
    legacy fallback，rank 15）。
    """
    if hours <= 1e-9:
        return
    school_year, _ = resolve_current_academic_term(
        target_date=target_date, session=session
    )
    row = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == emp_id,
            LeaveQuota.leave_type == "compensatory",
            LeaveQuota.school_year == school_year,
        )
        .first()
    )
    if row is None:
        row = (
            session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp_id,
                LeaveQuota.leave_type == "compensatory",
                LeaveQuota.school_year.is_(None),
                LeaveQuota.year == target_date.year,
            )
            .first()
        )
    if row is not None:
        row.total_hours = max(0.0, float(row.total_hours or 0) - float(hours))


logger = logging.getLogger(__name__)


def expire_comp_leave_grants(today: date, session: Session) -> dict:
    """撈到期 grant 結算，寫 SalaryRecord.unused_leave_payout + log。

    Args:
        today: 執行日期（scheduler 傳入；測試可注入任意日期）
        session: SQLAlchemy session（呼叫端管理生命週期）

    Returns:
        {
            'paid_employees': int,       # 有實際發放金額的員工數
            'total_amount': float,       # 全部發放金額合計
            'expired_grant_count': int,  # 本次 mark expired 的 grant 總數（含全消耗）
        }
    """
    # 撈所有到期且在職員工的 active grant（join 過濾 is_active=False）
    # order_by id 確保 FIFO 順序，meta.expired_grant_ids 結果可預期
    expired_grants = (
        session.query(OvertimeCompLeaveGrant)
        .join(Employee, Employee.id == OvertimeCompLeaveGrant.employee_id)
        .filter(
            OvertimeCompLeaveGrant.status == "active",
            OvertimeCompLeaveGrant.expires_at <= today,
            Employee.is_active.is_(True),
        )
        .order_by(OvertimeCompLeaveGrant.id)
        # row lock：只鎖 grant 列（join 的 Employee 不鎖），與 consume/release 序列化，
        # 避免 expiry 讀到舊 consumed_hours → 把幽靈未消耗額度換算成 payout 金額。
        .with_for_update(of=OvertimeCompLeaveGrant)
        .all()
    )

    # 依員工分組
    grants_by_emp: dict[int, list[OvertimeCompLeaveGrant]] = {}
    for g in expired_grants:
        grants_by_emp.setdefault(g.employee_id, []).append(g)

    total_paid = Decimal("0")
    paid_emp_count = 0

    for emp_id, grants in grants_by_emp.items():
        try:
            # rank 12：員工有待審補休假時，本輪延後到期結算。pending 假尚未消耗 grant
            # （consumed 只在核准時 +）；此時若折現未消耗時數，假後續核准會找不到 active
            # grant（FIFO 只取 active）→ 轉嫁別筆 active grant（雙得）或 422。延後到假單
            # 核准/駁回後下一輪再處理（grant 維持 active，expires_at 仍 <= today，下輪會再撈）。
            pending_comp_hours = float(
                session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
                .filter(
                    LeaveRecord.employee_id == emp_id,
                    LeaveRecord.leave_type == "compensatory",
                    LeaveRecord.status == "pending",
                )
                .scalar()
                or 0
            )
            if pending_comp_hours > 1e-9:
                logger.info(
                    "emp=%d 有待審補休 %.1fh，本輪延後補休到期結算"
                    "（待假單核准/駁回後下輪處理）",
                    emp_id,
                    pending_comp_hours,
                )
                continue
            with session.begin_nested():
                # 計算未消耗時數合計
                unexpired_hours = sum(
                    g.granted_hours - g.consumed_hours for g in grants
                )

                if unexpired_hours < 1e-9:
                    # 全用完：mark expired，不建 log，不計入 paid
                    # < 1e-9 防 float FIFO 多次扣抵尾數 underflow
                    for g in grants:
                        g.status = "expired"
                        g.expired_at = datetime.now(_TAIPEI_TZ)
                    continue

                # 取得員工時薪
                emp = session.get(Employee, emp_id)
                hourly_wage = _resolve_hourly_wage(emp, today)

                # 計算應發金額（HALF_UP 進位至整數元）
                raw_amount = calculate_unused_leave_compensation(
                    unexpired_hours, hourly_wage
                )
                amount = Decimal(str(round_half_up(raw_amount)))

                # 決定寫入目標月
                period_year, period_month = _next_month(today)

                # 建立 payout log
                log = UnusedLeavePayoutLog(
                    employee_id=emp_id,
                    source_type="comp_grant_expiry",
                    source_ref_id=None,
                    hours=unexpired_hours,
                    hourly_wage=Decimal(str(hourly_wage)),
                    amount=amount,
                    wage_basis_date=today,
                    salary_period_year=period_year,
                    salary_period_month=period_month,
                    meta={
                        "expired_grant_ids": [g.id for g in grants],
                        "hours_breakdown": [
                            {
                                "grant_id": g.id,
                                "overtime_date": g.granted_at.isoformat(),
                                "unexpired_hours": g.granted_hours - g.consumed_hours,
                            }
                            for g in grants
                        ],
                    },
                )
                session.add(log)
                session.flush()  # 取得 log.id 供 grant 反向綁定

                # Layer 1：SalaryRecord 存在且未 finalize → 直寫
                salary_record = _find_or_none_salary_record(
                    emp_id, period_year, period_month, session
                )
                sr_writeable = salary_record is not None and not getattr(
                    salary_record, "is_finalized", False
                )

                if sr_writeable:
                    salary_record.unused_leave_payout = (
                        Decimal(str(salary_record.unused_leave_payout or 0))
                    ) + amount
                    log.salary_record_id = salary_record.id
                # else：Layer 2 — log.salary_record_id 保持 None

                # 更新所有 grant 狀態
                payout_sr_id = salary_record.id if sr_writeable else None
                for g in grants:
                    g.status = "expired"
                    g.expired_at = datetime.now(_TAIPEI_TZ)
                    g.payout_log_id = log.id
                    g.payout_salary_record_id = payout_sr_id
                    # rank 11：折現未消耗時數後同步扣減補休 LeaveQuota，避免幽靈額度
                    # （檢查放行、消耗 FIFO 找不到 active grant → 422）。
                    _decrement_comp_quota(
                        session,
                        emp_id,
                        g.granted_at,
                        g.granted_hours - g.consumed_hours,
                    )

                total_paid += amount
                paid_emp_count += 1

        except Exception:
            logger.exception("expire_comp_leave failed for emp=%d", emp_id)

    return {
        "paid_employees": paid_emp_count,
        "total_amount": float(total_paid),
        "expired_grant_count": len(expired_grants),
    }
