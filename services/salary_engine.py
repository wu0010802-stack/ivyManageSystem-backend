"""
薪資計算引擎 - Re-export hub

此模組為向下相容的 re-export 入口。
所有實作已移至 services/salary/ 子模組。
外部 import（main.py, api/salary.py, tests/ 等）無需修改。

特殊說明：_compute_hourly_daily_hours 在此模組重新定義（非 import），
以保留測試中 `patch.object(se, 'MAX_DAILY_WORK_HOURS', ...)` 的可修補性。
"""

from datetime import datetime, timedelta
from typing import Optional

# 向下相容的公開介面
from services.salary.engine import SalaryEngine
from services.salary.hourly import _calc_lunch_overlap_hours, _calc_daily_hourly_pay
from services.salary.utils import get_working_days, get_bonus_distribution_month, get_meeting_deduction_period_start
from services.salary.constants import (
    MONTHLY_BASE_DAYS,
    MAX_DAILY_WORK_HOURS,
    HOURLY_OT1_RATE,
    HOURLY_OT2_RATE,
    HOURLY_REGULAR_HOURS,
    HOURLY_OT1_CAP_HOURS,
    LEAVE_DEDUCTION_RULES,
)
from services.salary.breakdown import SalaryBreakdown
from services.salary.proration import _prorate_base_salary, _prorate_for_period, _build_expected_workdays
from services.salary.utils import _sum_leave_deduction


def _compute_hourly_daily_hours(
    punch_in: datetime,
    punch_out: Optional[datetime],
    work_end_t,
) -> float:
    """計算時薪制員工單日實際工時（含午休扣除與時空穿越防護）。

    此函式定義於 re-export hub 使測試可透過
    ``patch.object(services.salary_engine, 'MAX_DAILY_WORK_HOURS', ...)``
    修改每日工時上限，維持向下相容。

    實作邏輯與 services.salary.hourly._compute_hourly_daily_hours 相同。
    """
    if punch_out is not None:
        effective_out = punch_out
    else:
        effective_out = datetime.combine(punch_in.date(), work_end_t)
        if effective_out <= punch_in:
            candidate = effective_out + timedelta(days=1)
            if (candidate - punch_in).total_seconds() / 3600 <= MAX_DAILY_WORK_HOURS:
                effective_out = candidate

    if effective_out <= punch_in:
        return 0.0

    diff = (effective_out - punch_in).total_seconds() / 3600
    overlap = sum(
        _calc_lunch_overlap_hours(punch_in, effective_out, _d)
        for _d in sorted({punch_in.date(), effective_out.date()})
    )
    diff -= overlap
    return max(0.0, min(diff, MAX_DAILY_WORK_HOURS))


__all__ = [
    "SalaryEngine",
    "SalaryBreakdown",
    "_compute_hourly_daily_hours",
    "_calc_lunch_overlap_hours",
    "_calc_daily_hourly_pay",
    "get_working_days",
    "get_bonus_distribution_month",
    "get_meeting_deduction_period_start",
    "MONTHLY_BASE_DAYS",
    "MAX_DAILY_WORK_HOURS",
    "HOURLY_OT1_RATE",
    "HOURLY_OT2_RATE",
    "HOURLY_REGULAR_HOURS",
    "HOURLY_OT1_CAP_HOURS",
    "LEAVE_DEDUCTION_RULES",
    "_prorate_base_salary",
    "_prorate_for_period",
    "_build_expected_workdays",
    "_sum_leave_deduction",
]
