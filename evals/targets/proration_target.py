"""Target: services/salary/proration.py 的三個純函式。

被攻擊對象:
- _prorate_base_salary(contracted_base, hire_date_raw, year, month)
- _prorate_for_period(contracted_base, hire_date_raw, resign_date_raw, year, month)
- _build_expected_workdays(year, month, holiday_set, daily_shift_map, ...)

不變量(共 12 條):

[_prorate_base_salary / _prorate_for_period 共用]
- IV1 nonneg: result >= 0
- IV2 capped: result <= contracted_base
- IV3 zero_base: contracted_base == 0 → result == 0

[_prorate_base_salary 專屬]
- IV4 no_hire_full: hire_date None → result == contracted_base
- IV5 future_hire_zero: hire 晚於 (year, month) → result == 0
- IV6 day_one_full: 同月入職 day <= 1 → result == contracted_base

[_prorate_for_period 專屬]
- IV7 resign_before_hire: 同月內 resign < hire(資料異常)→ result 不應為負,
   應該被偵測為錯誤或回 0(否則可能算出 -X 元退薪)
- IV8 already_resigned_zero: resign 在 (year, month) 之前 → result == 0
- IV9 future_hire_period_zero: hire 晚於 (year, month) → result == 0

[_build_expected_workdays 專屬]
- IV10 within_month: 結果中所有日期年月 == (year, month)
- IV11 invalid_month_raises: month ∉ [1, 12] → ValueError
- IV12 no_holiday_in_result: holiday_set 內的日期不在結果
"""

from __future__ import annotations

from datetime import date

from services.salary.proration import (
    _build_expected_workdays,
    _prorate_base_salary,
    _prorate_for_period,
)

from evals.core.target import Invariant, Target


def _runner(case: dict) -> dict:
    fn = case["fn"]
    if fn == "prorate_base":
        result = _prorate_base_salary(
            case["contracted_base"],
            case.get("hire_date_raw"),
            case["year"],
            case["month"],
        )
        return {"fn": fn, "value": result}
    if fn == "prorate_period":
        result = _prorate_for_period(
            case["contracted_base"],
            case.get("hire_date_raw"),
            case.get("resign_date_raw"),
            case["year"],
            case["month"],
        )
        return {"fn": fn, "value": result}
    if fn == "build_workdays":
        result = _build_expected_workdays(
            case["year"],
            case["month"],
            case.get("holiday_set", set()),
            case.get("daily_shift_map", {}),
            hire_date_raw=case.get("hire_date_raw"),
            resign_date_raw=case.get("resign_date_raw"),
            today=case.get("today"),
            makeup_set=case.get("makeup_set"),
        )
        return {"fn": fn, "value": sorted(result)}
    raise ValueError(f"unknown fn: {fn}")


# ─────────── shared invariants for prorate_* ───────────


def _is_prorate(case):
    return case.get("fn") in ("prorate_base", "prorate_period")


def _iv_nonneg(case, outcome):
    if not _is_prorate(case) or not outcome.get("ok"):
        return None
    v = outcome["result"]["value"]
    if v < 0:
        return f"result={v} 為負(contracted_base={case.get('contracted_base')})"
    return None


def _iv_capped(case, outcome):
    if not _is_prorate(case) or not outcome.get("ok"):
        return None
    v = outcome["result"]["value"]
    base = case.get("contracted_base", 0)
    if not isinstance(base, (int, float)) or base < 0:
        return None
    if v > base + 1e-6:
        return f"result={v} > contracted_base={base}"
    return None


def _iv_zero_base(case, outcome):
    if not _is_prorate(case) or not outcome.get("ok"):
        return None
    if case.get("contracted_base") == 0 and outcome["result"]["value"] != 0:
        return f"contracted_base=0 但 result={outcome['result']['value']}"
    return None


# ─────────── _prorate_base_salary 專屬 ───────────


def _iv_no_hire_full(case, outcome):
    if case.get("fn") != "prorate_base" or not outcome.get("ok"):
        return None
    if case.get("hire_date_raw") is None:
        v = outcome["result"]["value"]
        base = case.get("contracted_base", 0)
        if v != base:
            return f"hire_date=None 應回 contracted_base={base},實得 {v}"
    return None


def _iv_future_hire_zero(case, outcome):
    if case.get("fn") != "prorate_base" or not outcome.get("ok"):
        return None
    hire = case.get("hire_date_raw")
    if not isinstance(hire, date):
        return None
    y, m = case["year"], case["month"]
    if (hire.year, hire.month) > (y, m):
        v = outcome["result"]["value"]
        if v != 0:
            return f"hire={hire} 晚於 ({y},{m}) 應回 0,實得 {v}"
    return None


def _iv_day_one_full(case, outcome):
    if case.get("fn") != "prorate_base" or not outcome.get("ok"):
        return None
    hire = case.get("hire_date_raw")
    if not isinstance(hire, date):
        return None
    y, m = case["year"], case["month"]
    if hire.year == y and hire.month == m and hire.day <= 1:
        v = outcome["result"]["value"]
        base = case.get("contracted_base", 0)
        if v != base:
            return f"hire 在當月 day<=1 應回全額 {base},實得 {v}"
    return None


# ─────────── _prorate_for_period 專屬 ───────────


def _iv_resign_before_hire(case, outcome):
    """偵測「resign 早於 hire」的資料異常 → 結果不應為負。"""
    if case.get("fn") != "prorate_period" or not outcome.get("ok"):
        return None
    hire = case.get("hire_date_raw")
    resign = case.get("resign_date_raw")
    if not (isinstance(hire, date) and isinstance(resign, date)):
        return None
    y, m = case["year"], case["month"]
    if hire.year == y and hire.month == m and resign.year == y and resign.month == m:
        if resign < hire:
            v = outcome["result"]["value"]
            # nonneg 已保;但如果 result > 0 表示沒偵測異常,業務上可能更想 raise
            if v < 0:
                return f"resign({resign}) < hire({hire}) 算出負薪 {v}"
    return None


def _iv_already_resigned_zero(case, outcome):
    if case.get("fn") != "prorate_period" or not outcome.get("ok"):
        return None
    resign = case.get("resign_date_raw")
    if not isinstance(resign, date):
        return None
    y, m = case["year"], case["month"]
    if (resign.year, resign.month) < (y, m):
        v = outcome["result"]["value"]
        if v != 0:
            return f"resign={resign} 早於 ({y},{m}) 應回 0,實得 {v}"
    return None


def _iv_future_hire_period_zero(case, outcome):
    if case.get("fn") != "prorate_period" or not outcome.get("ok"):
        return None
    hire = case.get("hire_date_raw")
    if not isinstance(hire, date):
        return None
    y, m = case["year"], case["month"]
    if (hire.year, hire.month) > (y, m):
        v = outcome["result"]["value"]
        if v != 0:
            return f"hire={hire} 晚於 ({y},{m}) 應回 0,實得 {v}"
    return None


# ─────────── _build_expected_workdays 專屬 ───────────


def _iv_within_month(case, outcome):
    if case.get("fn") != "build_workdays" or not outcome.get("ok"):
        return None
    y, m = case["year"], case["month"]
    days = outcome["result"]["value"]
    bad = [
        d for d in days if not (isinstance(d, date) and d.year == y and d.month == m)
    ]
    if bad:
        return f"workdays 含非本月日期: {bad[:3]}"
    return None


def _iv_invalid_month_raises(case, outcome):
    if case.get("fn") != "build_workdays":
        return None
    m = case.get("month")
    if not isinstance(m, int):
        return None
    if 1 <= m <= 12:
        return None
    exc = outcome.get("exception", "")
    if not exc or "ValueError" not in exc:
        return f"month={m} (越界) 應 raise ValueError"
    return None


def _iv_no_holiday_in_result(case, outcome):
    if case.get("fn") != "build_workdays" or not outcome.get("ok"):
        return None
    holidays = case.get("holiday_set", set())
    if not holidays:
        return None
    days = outcome["result"]["value"]
    leak = [d for d in days if d in holidays]
    if leak:
        return f"workdays 含 holiday: {leak[:3]}"
    return None


TARGET = Target(
    name="proration",
    description=(
        "services/salary/proration.py 三個純函式:月中入職/離職底薪折算"
        "與當月預期上班日集合建立。直接影響員工本月應領底薪。"
    ),
    signature={
        "fields": {
            "fn": {
                "type": "string",
                "enum": ["prorate_base", "prorate_period", "build_workdays"],
            },
            "contracted_base": {
                "type": "float",
                "boundary": [0, 1, 30000, 100000, -1, 1e9],
            },
            "hire_date_raw": {"type": "date"},
            "resign_date_raw": {"type": "date"},
            "year": {"type": "int", "boundary": [2025, 2026, 2027, 0, -1, 9999]},
            "month": {"type": "int", "boundary": [1, 6, 12, 0, 13, -1]},
            "today": {"type": "date"},
        },
        "notes": (
            "prorate_base 處理單純入職折算;prorate_period 同時處理入職與離職;"
            "build_workdays 建立應上班日集合(扣假日、未來日、入職前、離職後)。"
        ),
    },
    invariants=[
        Invariant("IV1_nonneg", "prorate result 永不為負", _iv_nonneg),
        Invariant("IV2_capped", "prorate result 不超過 contracted_base", _iv_capped),
        Invariant("IV3_zero_base", "contracted_base=0 → result=0", _iv_zero_base),
        Invariant("IV4_no_hire_full", "hire_date=None → 全額", _iv_no_hire_full),
        Invariant("IV5_future_hire_zero", "hire 晚於計算月 → 0", _iv_future_hire_zero),
        Invariant("IV6_day_one_full", "hire 在當月 day<=1 → 全額", _iv_day_one_full),
        Invariant(
            "IV7_resign_before_hire",
            "resign<hire(資料異常)→ 不應算出負薪",
            _iv_resign_before_hire,
        ),
        Invariant(
            "IV8_already_resigned_zero",
            "resign 早於計算月 → 0",
            _iv_already_resigned_zero,
        ),
        Invariant(
            "IV9_future_hire_period_zero",
            "hire 晚於計算月 → 0",
            _iv_future_hire_period_zero,
        ),
        Invariant(
            "IV10_within_month", "workdays 全在指定 (year,month) 內", _iv_within_month
        ),
        Invariant(
            "IV11_invalid_month_raises",
            "month 越界 → ValueError",
            _iv_invalid_month_raises,
        ),
        Invariant(
            "IV12_no_holiday_in_result",
            "holiday 不出現在 workdays",
            _iv_no_holiday_in_result,
        ),
    ],
    seed_cases=[
        # prorate_base: 月中入職
        {
            "fn": "prorate_base",
            "contracted_base": 30000,
            "hire_date_raw": date(2026, 5, 15),
            "year": 2026,
            "month": 5,
        },
        # prorate_period: 全月在職
        {
            "fn": "prorate_period",
            "contracted_base": 30000,
            "hire_date_raw": date(2026, 1, 1),
            "resign_date_raw": None,
            "year": 2026,
            "month": 5,
        },
        # build_workdays: 普通月
        {
            "fn": "build_workdays",
            "year": 2026,
            "month": 5,
            "holiday_set": {date(2026, 5, 1)},
            "daily_shift_map": {},
            "today": date(2026, 5, 31),
        },
    ],
    runner=_runner,
    allowed_exceptions=("ValueError",),
)
