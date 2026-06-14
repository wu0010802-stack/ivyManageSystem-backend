"""學年月曆工具。

提供逐月狀態判定(closed / in_progress / future)、學年月份列舉、
已封存月份清單、進行中月份,以及單月工作日(排除六日)。
所有判定以注入的 `today` 為基準,純函式、無 I/O,可單元測試。
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta


def _month_first_day(year: int, month: int) -> date:
    """回傳該月首日。"""
    return date(year, month, 1)


def _month_last_day(year: int, month: int) -> date:
    """回傳該月末日。"""
    return date(year, month, monthrange(year, month)[1])


def month_status(year: int, month: int, today: date) -> str:
    """判定 (year, month) 相對 `today` 的狀態。

    Returns:
        - "closed":整月已過(月末日 < today)。
        - "in_progress":today 落在該月內(月首 ≤ today ≤ 月末)。
        - "future":月首 > today(尚未開始)。
    """
    first = _month_first_day(year, month)
    last = _month_last_day(year, month)
    if last < today:
        return "closed"
    if first > today:
        return "future"
    return "in_progress"


def all_months(year_start: date, year_end: date) -> list[tuple[int, int]]:
    """列舉 [year_start, year_end] 內每個 (year, month),含起訖月。"""
    months: list[tuple[int, int]] = []
    y, m = year_start.year, year_start.month
    end_y, end_m = year_end.year, year_end.month
    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return months


def closed_months(year_start: date, today: date) -> list[tuple[int, int]]:
    """回傳自學年起日至 `today` 為止、狀態為 closed 的月份清單。"""
    result: list[tuple[int, int]] = []
    y, m = year_start.year, year_start.month
    while True:
        first = _month_first_day(y, m)
        if first > today:
            break
        if month_status(y, m, today) == "closed":
            result.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return result


def current_month(today: date) -> tuple[int, int]:
    """回傳 `today` 所在的 (year, month)。"""
    return (today.year, today.month)


def current_term(today: date) -> tuple[int, int]:
    """回傳 `today` 所在的 (school_year 民國, semester)。

    規則對齊 app ``utils.academic.resolve_current_academic_term``:
    上學期 8/1–隔年 1/31(semester 1)、下學期 2/1–7/31(semester 2)。
    term-container(班級/才藝課程/報名)以此 tag,才會出現在 app 的「當前學期」過濾。
    """
    y, m = today.year, today.month
    if m >= 8:
        return (y - 1911, 1)
    if m == 1:
        return (y - 1912, 1)
    return (y - 1912, 2)  # 2..7 月 = 下學期


def workdays(year: int, month: int, upto: date | None = None) -> list[date]:
    """回傳該月所有工作日(排除週六日)。

    Args:
        upto: 若提供,只回傳 ≤ upto 的工作日(用於進行中月份截到今天)。
    """
    last = _month_last_day(year, month)
    result: list[date] = []
    d = _month_first_day(year, month)
    while d <= last:
        if upto is not None and d > upto:
            break
        if d.weekday() < 5:  # 0=週一 .. 4=週五
            result.append(d)
        d += timedelta(days=1)
    return result
