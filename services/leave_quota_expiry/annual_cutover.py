"""特休週年 cutover：scheduler 主邏輯之二。

每日由 asyncio polling scheduler 呼叫 `cutover_annual_leave_anniversaries(today, session)`，
撈出所有「今日滿週年」且在職員工，依序：

1. 查「目前生效 period row」（`period_start <= today <= period_end` 且 period_end == today 表示今日到期）
2. 若 current period 存在：
   a. 計算未休時數 = total_hours - 已核准已用
   b. 若 unused > 0：寫 UnusedLeavePayoutLog (source_type='annual_anniversary')
   c. Layer 1：若目標月 SalaryRecord 存在且未 finalize，直寫 `unused_leave_payout += amount`
3. 若 current period 不存在（cold start）：cold_start_count++，不結算
4. 一律建新 period row：period_start=today, period_end=today+1y（2/29 fallback 2/28）

冪等性：
- partial unique index `uq_leave_quotas_emp_period_annual` 擋同日重複 INSERT
- 若 IntegrityError 代表該員工當日已跑過 → 吃掉 exception，savepoint rollback

每個員工用 savepoint（`session.begin_nested()`）隔離，單員工失敗不影響其他人。
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.leaves_quota import _calc_annual_leave_hours
from models.employee import Employee
from models.leave import LeaveQuota
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from services.leave_quota_expiry.helpers import (
    _add_one_year_with_feb29_handling,
    _approved_annual_used_in_period,
    _find_or_none_salary_record,
    _is_anniversary_today_sql,
    _next_month,
    _resolve_hourly_wage,
)
from services.salary.unused_leave_pay import calculate_unused_leave_compensation
from utils.rounding import round_half_up

logger = logging.getLogger(__name__)


def cutover_annual_leave_anniversaries(today: date, session: Session) -> dict:
    """跑「今日滿週年」員工：結算上一週年未休 + 建新一週年 row。

    Args:
        today: 執行日期（scheduler 傳入；測試可注入任意日期）
        session: SQLAlchemy session（呼叫端管理生命週期）

    Returns:
        {
            'paid_employees': int,         # 有實際發放金額的員工數
            'cold_start_employees': int,   # 無既有 period row（首次週年）員工數
            'total_amount': float,         # 全部發放金額合計
            'total_anniversaries': int,    # 本次命中週年的員工總數（含冷啟動）
        }
    """
    # 撈所有今日滿週年且在職、且已滿 180 天的員工
    candidates = (
        session.query(Employee)
        .filter(
            Employee.is_active.is_(True),
            _is_anniversary_today_sql(Employee.hire_date, today),
            Employee.hire_date <= today - timedelta(days=180),
        )
        .all()
    )

    paid_count = 0
    cold_start_count = 0
    total_paid = Decimal("0")

    for emp in candidates:
        try:
            with session.begin_nested():
                # 撈今日到期的 period row
                # period_end == today（今日到期）或 period_end >= today（包含寬容查詢）
                # 因 period_end 是週年日當天（exclusive 起算），
                # 上一週期 period_end 恰好等於 today（今日滿週年）
                current = (
                    session.query(LeaveQuota)
                    .filter(
                        LeaveQuota.employee_id == emp.id,
                        LeaveQuota.leave_type == "annual",
                        LeaveQuota.period_start.isnot(None),
                        LeaveQuota.period_start <= today,
                        LeaveQuota.period_end >= today,  # 今日到期：period_end == today
                    )
                    .first()
                )

                if current is not None:
                    # 計算已核准已用時數
                    used = _approved_annual_used_in_period(
                        emp.id, current.period_start, today, session
                    )
                    unused = max(0.0, current.total_hours - used)

                    if unused > 0:
                        hourly_wage = _resolve_hourly_wage(emp, today)
                        raw_amount = calculate_unused_leave_compensation(
                            unused, hourly_wage
                        )
                        amount = Decimal(str(round_half_up(raw_amount)))

                        period_year, period_month = _next_month(today)

                        log = UnusedLeavePayoutLog(
                            employee_id=emp.id,
                            source_type="annual_anniversary",
                            source_ref_id=current.id,
                            hours=unused,
                            hourly_wage=Decimal(str(hourly_wage)),
                            amount=amount,
                            wage_basis_date=today,
                            salary_period_year=period_year,
                            salary_period_month=period_month,
                            meta={
                                "period_start": current.period_start.isoformat(),
                                "period_end": current.period_end.isoformat(),
                                "entitled_hours": current.total_hours,
                                "used_hours": used,
                            },
                        )
                        session.add(log)
                        session.flush()  # 取得 log.id

                        # Layer 1：SalaryRecord 存在且未 finalize → 直寫
                        salary_record = _find_or_none_salary_record(
                            emp.id, period_year, period_month, session
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

                        paid_count += 1
                        total_paid += amount
                else:
                    cold_start_count += 1

                # 一律建新 period row（不論是否有既有 period）
                new_period_end = _add_one_year_with_feb29_handling(today)
                hours = _calc_annual_leave_hours(
                    emp.hire_date, year=today.year, reference_date=today
                )
                session.add(
                    LeaveQuota(
                        employee_id=emp.id,
                        year=today.year,
                        school_year=None,
                        leave_type="annual",
                        total_hours=hours,
                        period_start=today,
                        period_end=new_period_end,
                        note=f"週年制配額（hire_date 基準 {emp.hire_date.isoformat()}）",
                    )
                )
                # flush 在此觸發 unique idx 違反（同日重跑），由 IntegrityError catch 處理
                session.flush()

        except IntegrityError:
            # partial unique idx uq_leave_quotas_emp_period_annual 擋同日重跑
            # begin_nested() context manager 已自動 rollback savepoint
            logger.info(
                "cutover_annual: emp=%d today=%s 已存在 period row，跳過",
                emp.id,
                today.isoformat(),
            )
        except Exception:
            logger.exception("cutover_annual failed for emp=%d", emp.id)

    return {
        "paid_employees": paid_count,
        "cold_start_employees": cold_start_count,
        "total_amount": float(total_paid),
        "total_anniversaries": len(candidates),
    }
