"""
薪資折算計算（月中入職/離職按在職天數比例折算）
"""

from datetime import date, datetime
from typing import Optional, Set


def _to_date(raw) -> Optional[date]:
    """將 str / datetime / date 正規化為 date，無法轉換時回傳 None。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw, '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _prorate_base_salary(contracted_base: float, hire_date_raw, year: int, month: int) -> float:
    """
    月中入職者：按「在職天數 ÷ 當月天數」比例折算本月應領底薪。

    規則：
    - 入職日為計算月份的 2 日（含）以後 → 按自然日比例折算
    - 入職日為 1 日或更早（上月/更早入職） → 全額，不折算
    - 入職日為非計算月份 → 全額，不折算

    ⚠️  注意：本方法「僅」影響 breakdown.base_salary（當月應領底薪顯示）。
        加班費時薪計算基準應以「完整契約月薪（emp.base_salary）÷ 30 ÷ 8」計算，
        絕不使用本方法回傳的折算後金額，否則會造成「雙重縮水」違反勞基法：
          錯誤：折算後底薪（15,000）/ 30 / 8 = 62.5 NTD/hr
          正確：契約月薪（30,000）  / 30 / 8 = 125.0 NTD/hr
    """
    import calendar as _cal
    if not contracted_base:
        return 0.0
    if not hire_date_raw:
        return contracted_base

    hire_d = _to_date(hire_date_raw)
    if hire_d is None:
        return contracted_base

    # 僅當入職年月與計算月份相同且非月初（day > 1）才折算
    if hire_d.year != year or hire_d.month != month or hire_d.day <= 1:
        return contracted_base

    _, month_days = _cal.monthrange(year, month)
    worked_days = month_days - hire_d.day + 1   # 入職日當天計入
    return contracted_base * worked_days / month_days


def _prorate_for_period(
    contracted_base: float,
    hire_date_raw,
    resign_date_raw,
    year: int,
    month: int,
) -> float:
    """
    計算當月實際在職天數的底薪折算（同時處理入職與離職）。

    規則：
    - hire_day >= 2（本月入職）：start_day = hire_day
    - resign_day < 月末（本月離職）：end_day = resign_day
    - 兩者均無異動：全額
    worked_days = end_day - start_day + 1
    result = contracted_base × worked_days / month_days

    ⚠️  注意：本方法「僅」影響 breakdown.base_salary（當月應領底薪顯示）。
        加班費時薪基準仍應使用完整契約月薪，避免「雙重縮水」。
    """
    import calendar as _cal
    if not contracted_base:
        return 0.0

    _, month_days = _cal.monthrange(year, month)
    start_day = 1
    end_day = month_days

    hire_d = _to_date(hire_date_raw)
    if hire_d and hire_d.year == year and hire_d.month == month and hire_d.day >= 2:
        start_day = hire_d.day

    resign_d = _to_date(resign_date_raw)
    if resign_d and resign_d.year == year and resign_d.month == month and resign_d.day < month_days:
        end_day = resign_d.day

    if start_day == 1 and end_day == month_days:
        return contracted_base  # 全額，無折算

    worked_days = end_day - start_day + 1
    return contracted_base * worked_days / month_days


def _build_expected_workdays(
    year: int,
    month: int,
    holiday_set: set,
    daily_shift_map: dict,
    hire_date_raw=None,
    resign_date_raw=None,
    today: "Optional[date]" = None,
) -> Set[date]:
    """
    建立指定月份的預期上班日集合。

    規則（優先順序由高至低）：
    1. 未來日期（> today）不計
    2. 假日（holiday_set）不計
    3. 有排班記錄且 shift_type_id 非 None → 應上班
    4. 無排班記錄 → 預設平日（週一～週五）
    5. hire_date_raw：入職前不計
    6. resign_date_raw：離職後不計
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month 必須介於 1–12，收到 {month!r}")

    import calendar as _cal
    if today is None:
        today = date.today()

    # 預先解析 hire/resign 日期，避免逐日比較時重複轉型
    hire_d = _to_date(hire_date_raw)
    resign_d = _to_date(resign_date_raw)

    expected_workdays: Set[date] = set()
    for day_num, weekday in _cal.Calendar().itermonthdays2(year, month):
        if day_num == 0:
            continue
        d = date(year, month, day_num)
        if d > today:
            continue
        if d in holiday_set:
            continue
        if hire_d and d < hire_d:
            continue
        if resign_d and d > resign_d:
            continue
        if d in daily_shift_map:
            if daily_shift_map[d] is not None:
                expected_workdays.add(d)
        else:
            if weekday < 5:
                expected_workdays.add(d)

    return expected_workdays
