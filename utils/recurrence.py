"""行事曆重複事件純函式 expander。

設計：
- 無 DB 依賴；輸入 event_date/end_date/rule/window，輸出 (start, end) tuple list
- 三種 rule type：weekly / monthly_day / monthly_nth
- weekday 用 Python 約定：0=Mon..6=Sun（matches `date.weekday()`）
- until inclusive；event_date 必為第一個 occurrence
- 多日事件 (end_date != None) 展開時每 occurrence 保持 `end_date - event_date` 長度
"""

import calendar as _cal
from datetime import date, timedelta

MAX_DURATION_DAYS = 730


def validate_rule(event_date: date, rule: dict) -> None:
    """驗證 rule 結構 + 業務規則；違反 raise ValueError。

    呼叫方應在 API 入口（events.py create/update）catch 並回 422。
    """
    rtype = rule.get("type")
    if rtype not in ("weekly", "monthly_day", "monthly_nth"):
        raise ValueError(f"unknown rule type: {rtype}")

    until_str = rule.get("until")
    if not until_str:
        raise ValueError("rule.until is required")
    try:
        until = date.fromisoformat(until_str)
    except ValueError as e:
        raise ValueError(f"rule.until invalid date: {until_str}") from e

    if until <= event_date:
        raise ValueError("rule.until must be after event_date")

    if (until - event_date).days > MAX_DURATION_DAYS:
        raise ValueError(
            f"rule.until - event_date exceeds {MAX_DURATION_DAYS} days (runaway 防護)"
        )

    if rtype == "weekly":
        wd = rule.get("weekday")
        if not isinstance(wd, int) or not (0 <= wd <= 6):
            raise ValueError("weekly.weekday must be int 0..6")
        if event_date.weekday() != wd:
            raise ValueError(
                f"event_date weekday ({event_date.weekday()}) does not match rule.weekday ({wd})"
            )
    elif rtype == "monthly_day":
        d = rule.get("day")
        if not isinstance(d, int) or not (1 <= d <= 31):
            raise ValueError("monthly_day.day must be int 1..31")
        if event_date.day != d:
            raise ValueError(
                f"event_date.day ({event_date.day}) does not match rule.day ({d})"
            )
    elif rtype == "monthly_nth":
        nth = rule.get("nth")
        if not isinstance(nth, int) or not (nth in (-1, 1, 2, 3, 4, 5)):
            raise ValueError("monthly_nth.nth must be int in {-1, 1..5}")
        wd = rule.get("weekday")
        if not isinstance(wd, int) or not (0 <= wd <= 6):
            raise ValueError("monthly_nth.weekday must be int 0..6")
        if event_date.weekday() != wd:
            raise ValueError(
                f"event_date weekday ({event_date.weekday()}) does not match rule.weekday ({wd})"
            )


def expand_event(
    event_date: date,
    end_date: date | None,
    rule: dict | None,
    window_from: date,
    window_to: date,
) -> list[tuple[date, date]]:
    """展開事件成 [(start, end), ...]，只回 window 內 occurrence。"""
    if rule is None:
        single_end = end_date or event_date
        if event_date > window_to or single_end < window_from:
            return []
        return [(event_date, single_end)]

    until = date.fromisoformat(rule["until"])
    duration = (end_date - event_date) if end_date else timedelta(0)
    rtype = rule["type"]

    if rtype == "weekly":
        return _expand_weekly(
            event_date, duration, rule["weekday"], until, window_from, window_to
        )
    if rtype == "monthly_day":
        return _expand_monthly_day(
            event_date, duration, rule["day"], until, window_from, window_to
        )
    if rtype == "monthly_nth":
        return _expand_monthly_nth(
            event_date,
            duration,
            rule["nth"],
            rule["weekday"],
            until,
            window_from,
            window_to,
        )
    raise ValueError(f"unknown rule type: {rtype}")


def _expand_weekly(
    event_date: date,
    duration: timedelta,
    weekday: int,
    until: date,
    wf: date,
    wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = event_date
    while cur <= until:
        end = cur + duration
        if cur <= wt and end >= wf:
            out.append((cur, end))
        cur = cur + timedelta(days=7)
    return out


def _expand_monthly_day(
    event_date: date,
    duration: timedelta,
    day: int,
    until: date,
    wf: date,
    wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    y, m = event_date.year, event_date.month
    while True:
        try:
            cur = date(y, m, day)
        except ValueError:
            cur = None
        if cur is not None:
            if cur > until:
                break
            end = cur + duration
            if cur <= wt and end >= wf:
                out.append((cur, end))
        m += 1
        if m > 12:
            m = 1
            y += 1
        if date(y, m, 1) > until:
            break
    return out


def _expand_monthly_nth(
    event_date: date,
    duration: timedelta,
    nth: int,
    weekday: int,
    until: date,
    wf: date,
    wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    y, m = event_date.year, event_date.month
    while True:
        cur = _nth_weekday_of_month(y, m, nth, weekday)
        if cur is not None:
            if cur > until:
                break
            end = cur + duration
            if cur <= wt and end >= wf:
                out.append((cur, end))
        m += 1
        if m > 12:
            m = 1
            y += 1
        if date(y, m, 1) > until:
            break
    return out


def _nth_weekday_of_month(year: int, month: int, nth: int, weekday: int) -> date | None:
    _, last_day = _cal.monthrange(year, month)
    matches = [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if date(year, month, d).weekday() == weekday
    ]
    if not matches:
        return None
    if nth == -1:
        return matches[-1]
    idx = nth - 1
    if 0 <= idx < len(matches):
        return matches[idx]
    return None
