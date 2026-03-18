"""
薪資計算引擎 - Re-export hub

此模組為向下相容的 re-export 入口。
所有實作已移至 services/salary/ 子模組。
外部 import（main.py, api/salary.py, tests/ 等）無需修改。
"""

from typing import Optional

# 向下相容的公開介面
from services.salary.engine import SalaryEngine
from services.salary.hourly import (
    _calc_lunch_overlap_hours,
    _calc_daily_hourly_pay,
    _compute_hourly_daily_hours as _hourly_compute,
)
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
    punch_in,
    punch_out: Optional[object],
    work_end_t,
) -> float:
    """薄包裝，使測試可透過 patch.object(services.salary_engine, 'MAX_DAILY_WORK_HOURS', ...) 調整上限。

    實作委派至 services.salary.hourly._compute_hourly_daily_hours。
    """
    return _hourly_compute(punch_in, punch_out, work_end_t, max_hours=MAX_DAILY_WORK_HOURS)


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
