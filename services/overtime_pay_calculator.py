"""services/overtime_pay_calculator.py — 加班費計算（勞基法）。

從 api/overtimes.py 抽出純函式，供 admin / portal 兩端共用，
避免 api/portal/overtimes.py 在 handler 內 lazy `from api.overtimes
import calculate_overtime_pay`（F1 第二波）。

公開：
- calculate_overtime_pay(base_salary, hours, overtime_type)

依勞基法基準：時薪 = 月薪 ÷ 30 ÷ 8；不同類型套不同倍率。
"""

from fastapi import HTTPException

from utils.constants import (
    DAILY_WORK_HOURS,
    HOLIDAY_RATE,
    MAX_OVERTIME_HOURS,
    RESTDAY_AFTER_8H_RATE,
    RESTDAY_FIRST_2H_RATE,
    RESTDAY_FIRST_SEGMENT,
    RESTDAY_MID_RATE,
    RESTDAY_MIN_HOURS,
    RESTDAY_SECOND_SEGMENT,
    WEEKDAY_AFTER_2H_RATE,
    WEEKDAY_FIRST_2H_RATE,
    WEEKDAY_THRESHOLD_HOURS,
)
from utils.rounding import round_half_up

MONTHLY_BASE_DAYS = 30  # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）


def calculate_overtime_pay(
    base_salary: float, hours: float, overtime_type: str
) -> float:
    """依勞基法計算加班費（時薪 = 月薪 ÷ 30 ÷ 8）。"""
    if not base_salary or base_salary <= 0:
        raise HTTPException(
            status_code=400,
            detail="該員工底薪未設定或為 0，無法計算加班費，請先完成薪資設定。",
        )
    # 防禦縱深：即使前端驗證被繞過，也不允許負數或零時數計算
    if hours <= 0:
        return 0.0
    hours = min(hours, MAX_OVERTIME_HOURS)
    hourly_base = base_salary / MONTHLY_BASE_DAYS / DAILY_WORK_HOURS

    if overtime_type == "weekday":
        # 平日：前2h × 1.34，超過 × 1.67
        if hours <= WEEKDAY_THRESHOLD_HOURS:
            return round_half_up(hourly_base * hours * WEEKDAY_FIRST_2H_RATE)
        return round_half_up(
            hourly_base * WEEKDAY_THRESHOLD_HOURS * WEEKDAY_FIRST_2H_RATE
            + hourly_base * (hours - WEEKDAY_THRESHOLD_HOURS) * WEEKDAY_AFTER_2H_RATE
        )
    elif overtime_type == "weekend":
        # 休息日：最低計 2h，前2h × 1.34，3~8h × 1.67，超8h × 2.67
        billable = max(hours, RESTDAY_MIN_HOURS)
        if billable <= RESTDAY_FIRST_SEGMENT:
            return round_half_up(hourly_base * billable * RESTDAY_FIRST_2H_RATE)
        elif billable <= RESTDAY_SECOND_SEGMENT:
            return round_half_up(
                hourly_base * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
                + hourly_base * (billable - RESTDAY_FIRST_SEGMENT) * RESTDAY_MID_RATE
            )
        return round_half_up(
            hourly_base * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
            + hourly_base
            * (RESTDAY_SECOND_SEGMENT - RESTDAY_FIRST_SEGMENT)
            * RESTDAY_MID_RATE
            + hourly_base * (billable - RESTDAY_SECOND_SEGMENT) * RESTDAY_AFTER_8H_RATE
        )
    else:
        # 例假日 / 國定假日：全部 × 2.0
        return round_half_up(hourly_base * hours * HOLIDAY_RATE)
