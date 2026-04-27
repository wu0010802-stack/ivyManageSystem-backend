"""
考勤扣款與基礎獎金計算（純函式層，無 SalaryEngine 狀態依賴）。

從 engine.py 拆出來以降低主類別行數並便於獨立測試／重用。
保持 API 完全向後相容：SalaryEngine.calculate_attendance_deduction /
SalaryEngine.calculate_bonus 仍可透過 delegation 呼叫。
"""

from __future__ import annotations

import logging

from services.attendance_parser import AttendanceResult

from .constants import MONTHLY_BASE_DAYS

logger = logging.getLogger(__name__)


def calculate_attendance_deduction(
    attendance: AttendanceResult,
    daily_salary: float = 0,
    base_salary: float = 0,
    late_details: list | None = None,
) -> dict:
    """計算考勤扣款。

    規則（業主 2026-04-25 確認：維持勞基法基準）：
    - 遲到/早退：按實際分鐘比例扣款（每分鐘 = 月薪 ÷ 30 ÷ 8 ÷ 60，勞基法基準）
      並設「單筆遲到/早退不超過當日日薪」上限，避免打卡異常造成超額扣款。
    - 未打卡：不扣款，僅記錄次數。
    - base_salary <= 0（時薪或未設定）→ 扣款皆為 0。

    Note: AttendancePolicy.late_deduction / early_leave_deduction /
    missing_punch_deduction 欄位已 deprecated，不影響本函式計算（DB 欄位保留
    以維持資料相容性，但 AttendancePolicyUpdate API 不再接受這些欄位）。
    """
    per_minute_rate = (
        base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 0
    )

    if late_details:
        late_minutes_per_day = late_details
    else:
        late_minutes_per_day = (
            [attendance.total_late_minutes] if attendance.total_late_minutes else []
        )
    late_deduction = sum(
        (
            min(m * per_minute_rate, daily_salary)
            if daily_salary > 0
            else m * per_minute_rate
        )
        for m in late_minutes_per_day
    )

    total_early_minutes = attendance.total_early_minutes
    early_count = attendance.early_leave_count or 0
    raw_early_deduction = total_early_minutes * per_minute_rate
    if daily_salary > 0 and early_count > 0:
        early_deduction = min(raw_early_deduction, early_count * daily_salary)
    else:
        early_deduction = raw_early_deduction

    missing_count = (
        attendance.missing_punch_in_count + attendance.missing_punch_out_count
    )

    return {
        "late_deduction": late_deduction,
        "missing_punch_deduction": 0,
        "early_leave_deduction": early_deduction,
        "late_count": attendance.late_count,
        "early_leave_count": attendance.early_leave_count,
        "missing_punch_count": missing_count,
        "total_late_minutes": attendance.total_late_minutes,
        "total_early_minutes": total_early_minutes,
    }


def calculate_bonus(
    target: int,
    current: int,
    base_amount: float,
    overtime_per: float = 500,
) -> dict:
    """舊版獎金計算（保留相容性）。

    festival_bonus = base_amount × (current / target)
    overtime_bonus = max(0, current - target) × overtime_per
    """
    if target <= 0:
        logger.warning(
            "calculate_bonus 收到 target=%s（<=0），獎金將歸零；請確認招生目標是否設定",
            target,
        )
        ratio = 0
    else:
        ratio = current / target
    festival_bonus = base_amount * ratio
    overtime_bonus = max(0, current - target) * overtime_per
    return {
        "festival_bonus": round(festival_bonus),
        "overtime_bonus": round(overtime_bonus),
        "ratio": ratio,
    }
