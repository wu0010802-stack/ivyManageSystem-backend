"""
薪資計算工具函式 - 請假扣款、工作天數、發放月判斷
"""

from datetime import date
from typing import Optional

from .constants import LEAVE_DEDUCTION_RULES, MONTHLY_BASE_DAYS


def _sum_leave_deduction(leaves, daily_salary: float) -> float:
    """計算請假扣款總額。

    優先使用 LeaveRecord.deduction_ratio 欄位；
    若為 None，fallback 至 LEAVE_DEDUCTION_RULES[leave_type]（向後相容舊資料）。

    Args:
        leaves:       LeaveRecord 列表（需有 leave_type, leave_hours, deduction_ratio 屬性）
        daily_salary: 日薪（base_salary / MONTHLY_BASE_DAYS）
    Returns:
        四捨五入後的扣款金額（整數）
    """
    total = 0.0
    for lv in leaves:
        ratio = lv.deduction_ratio if lv.deduction_ratio is not None \
            else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
        total += (lv.leave_hours / 8) * daily_salary * ratio
    return total


def get_working_days(year: int, month: int, session=None) -> int:
    """計算指定月份的法定工作日數（週一至週五，排除國定假日）"""
    if not 1 <= month <= 12:
        raise ValueError(f"month 必須介於 1–12，收到 {month!r}")
    import calendar
    from models.database import Holiday, get_session

    cal = calendar.Calendar()
    # 取得當月所有工作日（週一=0 到 週五=4）
    workdays = [d for d in cal.itermonthdays2(year, month)
                if d[0] != 0 and d[1] < 5]

    # 查詢當月國定假日
    _session = session or get_session()
    try:
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        holidays = _session.query(Holiday.date).filter(
            Holiday.date >= month_start,
            Holiday.date <= month_end,
            Holiday.is_active == True
        ).all()
        holiday_dates = {h.date for h in holidays}
    finally:
        if not session:
            _session.close()

    # 排除落在工作日的國定假日
    working_days = len([d for d in workdays if date(year, month, d[0]) not in holiday_dates])
    return working_days


def get_bonus_distribution_month(month: int) -> bool:
    """
    判斷是否為節慶獎金發放月
    2月 → 發放 12+1月
    6月 → 發放 2-5月
    9月 → 發放 6-8月
    12月 → 發放 9-11月
    """
    return month in (2, 6, 9, 12)


def get_meeting_deduction_period_start(year: int, month: int) -> Optional[date]:
    """
    返回發放月的會議缺席扣款起算日。
    計算範圍 = 上次發放月（不含，因其當月已計算）至當發放月（含）。

    2月  → 1月1日  （上次發放為12月，1月為未扣款的非發放月）
    6月  → 3月1日  （上次發放為2月，3–5月為未扣款的非發放月）
    9月  → 7月1日  （上次發放為6月，7–8月為未扣款的非發放月）
    12月 → 10月1日 （上次發放為9月，10–11月為未扣款的非發放月）

    非發放月返回 None（不需要補查歷史記錄）。
    """
    if month == 2:
        return date(year, 1, 1)
    elif month == 6:
        return date(year, 3, 1)
    elif month == 9:
        return date(year, 7, 1)
    elif month == 12:
        return date(year, 10, 1)
    return None
