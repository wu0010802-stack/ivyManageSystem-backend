"""
salary package - 薪資計算領域子模組集合
"""

from .constants import (
    MONTHLY_BASE_DAYS,
    MAX_DAILY_WORK_HOURS,
    HOURLY_OT1_RATE,
    HOURLY_OT2_RATE,
    HOURLY_REGULAR_HOURS,
    HOURLY_OT1_CAP_HOURS,
    LEAVE_DEDUCTION_RULES,
    FESTIVAL_BONUS_BASE,
    TARGET_ENROLLMENT,
    OVERTIME_TARGET,
    OVERTIME_BONUS_PER_PERSON,
    SUPERVISOR_DIVIDEND,
    SUPERVISOR_FESTIVAL_BONUS,
    OFFICE_FESTIVAL_BONUS_BASE,
    POSITION_GRADE_MAP,
)
from .breakdown import SalaryBreakdown
from .hourly import _calc_lunch_overlap_hours, _compute_hourly_daily_hours, _calc_daily_hourly_pay
from .proration import _prorate_base_salary, _prorate_for_period, _build_expected_workdays
from .utils import _sum_leave_deduction, get_working_days, get_bonus_distribution_month, get_meeting_deduction_period_start, calc_daily_salary
from .festival import (
    get_position_grade,
    get_festival_bonus_base,
    get_target_enrollment,
    get_supervisor_dividend,
    get_supervisor_festival_bonus,
    get_office_festival_bonus_base,
    get_overtime_target,
    get_overtime_per_person,
    is_eligible_for_festival_bonus,
    calculate_overtime_bonus,
    calculate_festival_bonus_v2,
)
from .engine import SalaryEngine

__all__ = [
    "MONTHLY_BASE_DAYS",
    "MAX_DAILY_WORK_HOURS",
    "HOURLY_OT1_RATE",
    "HOURLY_OT2_RATE",
    "HOURLY_REGULAR_HOURS",
    "HOURLY_OT1_CAP_HOURS",
    "LEAVE_DEDUCTION_RULES",
    "FESTIVAL_BONUS_BASE",
    "TARGET_ENROLLMENT",
    "OVERTIME_TARGET",
    "OVERTIME_BONUS_PER_PERSON",
    "SUPERVISOR_DIVIDEND",
    "SUPERVISOR_FESTIVAL_BONUS",
    "OFFICE_FESTIVAL_BONUS_BASE",
    "POSITION_GRADE_MAP",
    "SalaryBreakdown",
    "_calc_lunch_overlap_hours",
    "_compute_hourly_daily_hours",
    "_calc_daily_hourly_pay",
    "_prorate_base_salary",
    "_prorate_for_period",
    "_build_expected_workdays",
    "_sum_leave_deduction",
    "get_working_days",
    "get_bonus_distribution_month",
    "get_meeting_deduction_period_start",
    "calc_daily_salary",
    "get_position_grade",
    "get_festival_bonus_base",
    "get_target_enrollment",
    "get_supervisor_dividend",
    "get_supervisor_festival_bonus",
    "get_office_festival_bonus_base",
    "get_overtime_target",
    "get_overtime_per_person",
    "is_eligible_for_festival_bonus",
    "calculate_overtime_bonus",
    "calculate_festival_bonus_v2",
    "SalaryEngine",
]
