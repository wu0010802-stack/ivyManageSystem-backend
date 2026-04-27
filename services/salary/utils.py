"""
薪資計算工具函式 - 請假扣款、工作天數、發放月判斷
"""

from datetime import date
from typing import Optional

from .constants import (
    LEAVE_DEDUCTION_RULES,
    MONTHLY_BASE_DAYS,
    SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS,
)
from services.workday_rules import classify_day, load_day_rule_maps


def is_attendance_waived(att) -> bool:
    """考勤異常是否已由管理員豁免（薪資端應視為不扣）。

    管理員在「考勤異常確認」頁面以 admin_waive 標記後，UI 顯示「管理員豁免」，
    薪資計算遲到/早退/缺打卡扣款時亦應排除該日，否則前台處理狀態與薪資結果分叉
    （員工以為被豁免、薪資仍照扣）。

    與 admin_accept 區別：admin_accept 是管理員代為承認異常（仍扣），
    admin_waive 才是真正豁免（不扣）。
    """
    return getattr(att, "confirmed_action", None) == "admin_waive"


def _sum_leave_deduction(
    leaves,
    daily_salary: float,
    ytd_sick_hours_before_month: float = 0.0,
) -> float:
    """計算請假扣款總額。

    優先使用 LeaveRecord.deduction_ratio 欄位；
    若為 None，fallback 至 LEAVE_DEDUCTION_RULES[leave_type]（向後相容舊資料）。

    勞基法第 43 條 + 勞工請假規則第 4 條：
    普通傷病假一年內累計未逾 30 日（240h）部分折半薪，超過之部分不給薪。
    `ytd_sick_hours_before_month` 提供本月之前、同一年度內已核准的病假時數，
    讓本月超過年度上限的病假改以 ratio=1.0（全扣）計算。人工覆寫的
    `deduction_ratio` 仍優先採用（尊重 HR 判斷），但該筆時數仍計入年度累計。

    Args:
        leaves:                      LeaveRecord 列表（需有 leave_type, leave_hours,
                                      deduction_ratio, start_date 屬性）
        daily_salary:                日薪（base_salary / MONTHLY_BASE_DAYS）
        ytd_sick_hours_before_month: 本月之前、同年度已核准病假時數（預設 0）
    Returns:
        扣款金額（浮點數，由呼叫端決定是否 round）
    """
    total = 0.0
    sick_used = float(ytd_sick_hours_before_month or 0.0)

    # 病假按 start_date 由早到晚處理（先請的先享半薪額度）
    sick_leaves = sorted(
        [lv for lv in leaves if lv.leave_type == "sick"],
        key=lambda lv: getattr(lv, "start_date", None) or date.min,
    )
    other_leaves = [lv for lv in leaves if lv.leave_type != "sick"]
    standard_sick_ratio = LEAVE_DEDUCTION_RULES.get("sick", 0.5)

    for lv in sick_leaves:
        hours = lv.leave_hours or 0
        # 僅「明確偏離標準 0.5」的 ratio 視為 HR 人工覆寫；
        # 核准流程會把 ratio 寫成標準值 0.5，此時應照常套用年度上限。
        is_genuine_override = (
            lv.deduction_ratio is not None and lv.deduction_ratio != standard_sick_ratio
        )
        if is_genuine_override:
            total += (hours / 8) * daily_salary * lv.deduction_ratio
        else:
            half_paid = max(
                0.0, min(SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS - sick_used, hours)
            )
            unpaid = hours - half_paid
            total += (half_paid / 8) * daily_salary * 0.5
            total += (unpaid / 8) * daily_salary * 1.0
        sick_used += hours

    for lv in other_leaves:
        ratio = (
            lv.deduction_ratio
            if lv.deduction_ratio is not None
            else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
        )
        total += (lv.leave_hours / 8) * daily_salary * ratio
    return total


def get_working_days(year: int, month: int, session=None) -> int:
    """計算指定月份的法定工作日數（含補班日，排除國定假日）"""
    if not 1 <= month <= 12:
        raise ValueError(f"month 必須介於 1–12，收到 {month!r}")
    import calendar
    from models.database import get_session

    # 查詢當月國定假日
    _session = session or get_session()
    try:
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        holiday_map, makeup_map = load_day_rule_maps(_session, month_start, month_end)
    finally:
        if not session:
            _session.close()

    working_days = 0
    for day in range(1, month_end.day + 1):
        current = date(year, month, day)
        if classify_day(current, holiday_map, makeup_map)["kind"] == "workday":
            working_days += 1
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


def get_current_period_passed_months(year: int, month: int) -> list[tuple[int, int]]:
    """
    回傳該月所屬發放期起點至查詢月（含）之 (year, month) 清單。
    發放月（2/6/9/12）輸入時回空 list。

    期間定義（對齊 get_meeting_deduction_period_start）：
      - 2 月發放 12(去年)、1
      - 6 月發放 2、3、4、5
      - 9 月發放 6、7、8
      - 12 月發放 9、10、11
    """
    if get_bonus_distribution_month(month):
        return []

    if month == 1:
        return [(year - 1, 12), (year, 1)]
    if 3 <= month <= 5:
        return [(year, m) for m in range(2, month + 1)]
    if 7 <= month <= 8:
        return [(year, m) for m in range(6, month + 1)]
    if 10 <= month <= 11:
        return [(year, m) for m in range(9, month + 1)]
    return []


def get_distribution_period_months(year: int, month: int) -> list[tuple[int, int]]:
    """發放月所結算的月份清單（不含發放月本身）。

    Why: 節慶獎金規則為「發放月時加總期間每月各自比例」（業主 2026-04-25 確認）；
    此 helper 提供 calculate_salary 在發放月時要 iterate 的目標月份清單。
    非發放月輸入回 []。

    對應關係：
      2 月  → [(year-1, 12), (year, 1)]   （與 get_current_period_passed_months(year, 1) 一致）
      6 月  → [(year, 2), (year, 3), (year, 4), (year, 5)]
      9 月  → [(year, 6), (year, 7), (year, 8)]
      12 月 → [(year, 9), (year, 10), (year, 11)]
    """
    if not get_bonus_distribution_month(month):
        return []
    if month == 2:
        return get_current_period_passed_months(year, 1)
    return get_current_period_passed_months(year, month - 1)


def calc_daily_salary(base_salary) -> float:
    """日薪計算：base_salary / 30（勞基法基準 MONTHLY_BASE_DAYS）"""
    return (base_salary or 0) / MONTHLY_BASE_DAYS


def mark_salary_stale(session, employee_id: int, year: int, month: int) -> bool:
    """將指定員工該月 SalaryRecord 標記為 needs_recalc=True。

    用於上游事件(假單/加班審核)後薪資重算失敗、批次重算 except 路徑等場景,
    確保 finalize 完整性檢查能擋下未成功重算的記錄。

    Returns:
        True  — 找到 record 並標記成功(caller 仍需自行 commit)
        False — 該月無 record(屬「從未算過」場景,由 finalize 的 missing 檢查擋下)
    """
    from models.database import SalaryRecord

    rec = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .first()
    )
    if rec is None:
        return False
    rec.needs_recalc = True
    return True


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
