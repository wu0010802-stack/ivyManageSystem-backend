"""Offline Claude 模式預先生成的攻擊 case。

設計原則:
每個 case 是 Claude(本對話的 LLM 自身)針對某個 target 的某條 invariant 思考出來的
adversarial input。每個 case 必帶:
- target_invariant: 試圖打破哪一條
- hypothesis: 為何認為這個 input 會打破
- input: 真正餵 runner 的 dict

跟 HeuristicAttacker 的根本差別:
- 這裡的 case 是「讀過 invariant 文字 + 理解 Python 語意陷阱」後挑的,
  不是窮舉邊界。
- 例:salary=NaN 與 salary=-0.0 都是「< 0 比較會回 False」的 Python 陷阱,
  heuristic 不會主動挑這兩個值組合。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

# 從 leave_policy_target 引入固定 today,確保 invariant 與 case 用同一個 reference
from evals.targets.leave_policy_target import FIXED_TODAY


def _leave_policy_cases() -> list[dict]:
    today = FIXED_TODAY
    return [
        # === IV1 calendar_days_consistency ===
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=10),
                "end_date": today + timedelta(days=10),
                "leave_hours": 8,
                "today": today,
            },
            "__target_invariant": "IV1_calendar_days",
            "__hypothesis": "同日請假 1 天,requires_doc 應為 False(boundary check at days+1=1)",
        },
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=10),
                "end_date": today + timedelta(days=11),
                "leave_hours": 16,
                "today": today,
            },
            "__target_invariant": "IV1_calendar_days",
            "__hypothesis": "兩日請假 days+1=2,requires_doc 應為 False(剛好不超過 2)",
        },
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=10),
                "end_date": today + timedelta(days=12),
                "leave_hours": 24,
                "today": today,
            },
            "__target_invariant": "IV1_calendar_days",
            "__hypothesis": "三日請假 days+1=3,requires_doc 應為 True(剛好超過 2)",
        },
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=11),
                "end_date": today + timedelta(days=10),  # end < start
                "leave_hours": 8,
                "today": today,
            },
            "__target_invariant": "IV1_calendar_days",
            "__hypothesis": (
                "end < start 會給負天數;requires_doc 應 False(負 > 2 為 False),"
                "但函式可能沒處理此情況"
            ),
        },
        # === IV2 personal_advance_blocks ===
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=1),
                "end_date": today + timedelta(days=1),
                "leave_hours": 8,
                "today": today,
            },
            "__target_invariant": "IV2_personal_advance_blocks",
            "__hypothesis": "事假提前 1 日(< 提前 2 日門檻),應 raise",
        },
        {
            "input": {
                "leave_type": "personal",
                "start_date": today,
                "end_date": today,
                "leave_hours": 8,
                "today": today,
            },
            "__target_invariant": "IV2_personal_advance_blocks",
            "__hypothesis": "事假當日提出,應 raise",
        },
        {
            "input": {
                "leave_type": "personal",
                "start_date": today - timedelta(days=1),
                "end_date": today,
                "leave_hours": 16,
                "today": today,
            },
            "__target_invariant": "IV2_personal_advance_blocks",
            "__hypothesis": "事假事後補(start 在過去),提前不足 2 日,應 raise",
        },
        # === IV3 personal_advance_ok (boundary) ===
        {
            "input": {
                "leave_type": "personal",
                "start_date": today + timedelta(days=2),
                "end_date": today + timedelta(days=2),
                "leave_hours": 8,
                "today": today,
            },
            "__target_invariant": "IV3_personal_advance_ok",
            "__hypothesis": "事假剛好 today+2(邊界),應通過",
        },
        # === IV4 sick_non_multiple_blocks ===
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": 4.0000000001,
                "today": today,
            },
            "__target_invariant": "IV4_sick_non_multiple_blocks",
            "__hypothesis": (
                "病假 hours = 4.0000000001(浮點略偏離 4 倍),"
                "% 4 != 0 應 raise;若程式做了 isclose 容差就會放行"
            ),
        },
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": float("nan"),
                "today": today,
            },
            "__target_invariant": "IV4_sick_non_multiple_blocks",
            "__hypothesis": (
                "hours=NaN:nan%4=nan, nan!=0 為 True → 應 raise;"
                "但若改成 isclose 或 == 0 容易意外通過"
            ),
        },
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": 3.999999999,
                "today": today,
            },
            "__target_invariant": "IV4_sick_non_multiple_blocks",
            "__hypothesis": "病假 hours = 3.999... < 4,% 4 = 3.999,應 raise",
        },
        # === IV5 sick_multiple_ok ===
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": -4,
                "today": today,
            },
            "__target_invariant": "IV5_sick_multiple_ok",
            "__hypothesis": (
                "Python -4 % 4 == 0 → 規則通過!但負數時數無業務意義;"
                "代表規則沒檢查 hours > 0,可能允許負時數請假"
            ),
        },
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": 0,
                "today": today,
            },
            "__target_invariant": "IV5_sick_multiple_ok",
            "__hypothesis": "0 % 4 == 0 → 規則通過,但 0 小時請假有意義嗎?(我的 IV5 已排除 hours>0)",
        },
        {
            "input": {
                "leave_type": "sick",
                "start_date": today,
                "end_date": today,
                "leave_hours": float("inf"),
                "today": today,
            },
            "__target_invariant": "IV4_sick_non_multiple_blocks",
            "__hypothesis": "hours=inf:inf%4=nan, !=0 為 True → 應 raise",
        },
    ]


def _insurance_cases() -> list[dict]:
    return [
        # === IV1 negative_salary_raises - Python 比較陷阱 ===
        {
            "input": {"salary": -0.0, "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV1_negative_salary_raises",
            "__hypothesis": (
                "Python: -0.0 < 0 為 False → 繞過 negative guard。"
                "我的 IV1 也許不會觸發(因 salary == 0),但這標誌規則有 silent 通過風險"
            ),
        },
        {
            "input": {"salary": float("nan"), "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV1_negative_salary_raises",
            "__hypothesis": (
                "Python: NaN < 0 為 False → 繞過 negative guard,calculate 繼續執行;"
                "get_bracket 走到 fallback last entry,後續 NaN 傳染所有保費"
            ),
        },
        {
            "input": {"salary": float("-inf"), "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV1_negative_salary_raises",
            "__hypothesis": "-inf < 0 為 True → 應 raise(對照組,確認 inf 異於 NaN)",
        },
        {
            "input": {"salary": float("inf"), "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV8_no_negative_premium",
            "__hypothesis": "+inf 通過 negative guard,走最高級距 cap;產出應為有限數(level 8 必然非負)",
        },
        # === IV2 invalid_pension_rate_raises - 比較陷阱 ===
        {
            "input": {
                "salary": 30000,
                "dependents": 0,
                "pension_self_rate": float("nan"),
            },
            "__target_invariant": "IV2_invalid_pension_rate_raises",
            "__hypothesis": (
                "Python: 0 <= NaN <= 0.06 兩邊都 False → not False = True → 確實 raise。"
                "對照組,確認 NaN 在這個寫法下不會 silent 通過"
            ),
        },
        {
            "input": {"salary": 30000, "dependents": 0, "pension_self_rate": -0.0},
            "__target_invariant": "IV2_invalid_pension_rate_raises",
            "__hypothesis": "-0.0 == 0,通過 guard;後續 -0.0 * salary = -0.0 round 到 0;不算違反",
        },
        {
            "input": {
                "salary": 30000,
                "dependents": 0,
                "pension_self_rate": 0.0600000001,
            },
            "__target_invariant": "IV2_invalid_pension_rate_raises",
            "__hypothesis": "略大於 0.06 邊界;應 raise(若用 isclose 會 silent 通過)",
        },
        # === IV3 / IV9 health_exempt ===
        {
            "input": {
                "salary": 30000,
                "dependents": 100,
                "pension_self_rate": 0,
                "health_exempt": True,
            },
            "__target_invariant": "IV9_health_exempt_overrides_deps",
            "__hypothesis": "極大 dependents + exempt;確認 dependents 的乘子被 exempt 完全覆蓋",
        },
        # === IV4 total_consistency 在 NaN 下的破口 ===
        {
            "input": {
                "salary": float("nan"),
                "dependents": 2,
                "pension_self_rate": 0.06,
            },
            "__target_invariant": "IV4_total_consistency",
            "__hypothesis": (
                "salary=NaN 進到 calculate,total_employee 與三項 sum 都會 NaN;"
                "abs(NaN-NaN)>0.5 為 False → IV4 silent 通過。"
                "代表 IV4 自身對 NaN 不健全,需加 finite 檢查"
            ),
        },
        # === IV5 / IV6 dependents 邊界 ===
        {
            "input": {"salary": 30000, "dependents": 1.5, "pension_self_rate": 0},
            "__target_invariant": "IV5_dependents_clamp_high",
            "__hypothesis": (
                "dependents=1.5(float 而非 int):程式 min(max(0, 1.5), 3) = 1.5,"
                "health_emp = base * 2.5(非整數眷屬乘子!);應該 reject 非整數"
            ),
        },
        {
            "input": {"salary": 30000, "dependents": True, "pension_self_rate": 0},
            "__target_invariant": "IV4_total_consistency",
            "__hypothesis": "Python bool 是 int;True 應與 dependents=1 等價",
        },
        {
            "input": {"salary": 30000, "dependents": 4, "pension_self_rate": 0},
            "__target_invariant": "IV5_dependents_clamp_high",
            "__hypothesis": "dependents=4 邊界 +1,確認與 dependents=3 同結果",
        },
        # === IV7 monotone — 級距邊界 ===
        {
            "input": {"salary": 25250, "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV7_monotone_insured",
            "__hypothesis": "級距邊界值,bracket 是 <=,確認 25250 落入該級距而非下一級",
        },
        {
            "input": {"salary": 25251, "dependents": 0, "pension_self_rate": 0},
            "__target_invariant": "IV7_monotone_insured",
            "__hypothesis": "級距邊界 +1,確認跳到下一級;與 25250 結果應遞增",
        },
        # === 分項投保 race(IV4 仍需成立) ===
        {
            "input": {
                "salary": 30000,
                "dependents": 2,
                "pension_self_rate": 0.06,
                "labor_insured": 0,  # 0 應視同 None(沿用 salary)
                "health_insured": 50000,
                "pension_insured": 200000,  # 超過 cap
            },
            "__target_invariant": "IV4_total_consistency",
            "__hypothesis": (
                "labor_insured=0 在 docstring 寫明視同 None;確認三制度獨立 cap 後 IV4 仍成立"
            ),
        },
        {
            "input": {
                "salary": 30000,
                "dependents": -5,
                "pension_self_rate": 0,
            },
            "__target_invariant": "IV6_dependents_clamp_low",
            "__hypothesis": "dependents 大負數,確認 clamp 到 0 與 dependents=0 同結果",
        },
    ]


def _proration_cases() -> list[dict]:
    return [
        # === IV5/IV9 future hire ===
        {
            "input": {
                "fn": "prorate_base",
                "contracted_base": 30000,
                "hire_date_raw": date(2026, 12, 1),
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV5_future_hire_zero",
            "__hypothesis": "hire 在計算月份的下半年,應回 0(避免補算歷史月發整月薪)",
        },
        {
            "input": {
                "fn": "prorate_period",
                "contracted_base": 30000,
                "hire_date_raw": date(2027, 1, 1),
                "resign_date_raw": None,
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV9_future_hire_period_zero",
            "__hypothesis": "hire 在隔年才入職,當月應 0",
        },
        # === IV7 resign before hire(資料異常)===
        {
            "input": {
                "fn": "prorate_period",
                "contracted_base": 30000,
                "hire_date_raw": date(2026, 5, 20),
                "resign_date_raw": date(2026, 5, 10),  # resign 早於 hire
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV7_resign_before_hire",
            "__hypothesis": (
                "資料異常 resign<hire 同月:start=20 end=10 → worked_days=-9 → "
                "薪資 -9/31 = 負值。invariant 抓 nonneg 應該 fire"
            ),
        },
        # === IV8 already resigned ===
        {
            "input": {
                "fn": "prorate_period",
                "contracted_base": 30000,
                "hire_date_raw": date(2026, 1, 1),
                "resign_date_raw": date(2026, 3, 31),  # 三月底已離職
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV8_already_resigned_zero",
            "__hypothesis": "三月底離職算五月薪,應 0(不補算歷史薪)",
        },
        # === IV6 day_one_full(boundary) ===
        {
            "input": {
                "fn": "prorate_base",
                "contracted_base": 30000,
                "hire_date_raw": date(2026, 5, 1),  # 1 號入職
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV6_day_one_full",
            "__hypothesis": "1 號入職應回全額(boundary day=1)",
        },
        # === IV2 capped:hire_date 為 invalid str ===
        {
            "input": {
                "fn": "prorate_base",
                "contracted_base": 30000,
                "hire_date_raw": "2026-13-99",  # 無效日期
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV4_no_hire_full",
            "__hypothesis": (
                "_to_date 對無效字串回 None,函式 fallback 全額;"
                "確認此 fallback 行為與 IV4(None=全額)是否一致"
            ),
        },
        # === IV1 nonneg:contracted_base 為負 ===
        {
            "input": {
                "fn": "prorate_base",
                "contracted_base": -30000,
                "hire_date_raw": date(2026, 5, 15),
                "year": 2026,
                "month": 5,
            },
            "__target_invariant": "IV1_nonneg",
            "__hypothesis": (
                "Python: `if not contracted_base:` 對 -30000 為 truthy(非 0)→ "
                "進入計算;結果 = -30000 × 17/31 ≈ -16451 為負(真 bug)"
            ),
        },
        # === IV11 invalid month ===
        {
            "input": {
                "fn": "build_workdays",
                "year": 2026,
                "month": 13,
                "holiday_set": set(),
                "daily_shift_map": {},
                "today": date(2026, 12, 31),
            },
            "__target_invariant": "IV11_invalid_month_raises",
            "__hypothesis": "month=13 應 raise ValueError(_build_expected_workdays 已加守衛)",
        },
        {
            "input": {
                "fn": "prorate_base",
                "contracted_base": 30000,
                "hire_date_raw": date(2026, 5, 15),
                "year": 2026,
                "month": 13,
            },
            "__target_invariant": "IV11_invalid_month_raises",
            "__hypothesis": (
                "month=13 餵 _prorate_base_salary:目前丟 IllegalMonthError 而非 "
                "ValueError(API 不一致,_build_expected_workdays 有守衛但這支沒)"
            ),
        },
        # === IV12 holiday excluded ===
        {
            "input": {
                "fn": "build_workdays",
                "year": 2026,
                "month": 5,
                "holiday_set": {date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5)},
                "daily_shift_map": {},
                "today": date(2026, 5, 31),
            },
            "__target_invariant": "IV12_no_holiday_in_result",
            "__hypothesis": "確認多個 holiday 全被排除",
        },
        # === IV10 within_month edge ===
        {
            "input": {
                "fn": "build_workdays",
                "year": 2026,
                "month": 2,  # 平年只有 28 天
                "holiday_set": set(),
                "daily_shift_map": {},
                "today": date(2026, 2, 28),
            },
            "__target_invariant": "IV10_within_month",
            "__hypothesis": "2026 是平年,2 月應只有 28 天,結果集中無 2/29",
        },
    ]


def _insurance_endpoint_cases() -> list[dict]:
    return [
        # === IV1/IV3:NaN salary ===
        {
            "input": {"salary": "NaN", "dependents": 0},
            "__target_invariant": "IV1_no_5xx",
            "__hypothesis": "?salary=NaN 進到 service 已修補 raise ValueError;endpoint 是否 catch 為 4xx?",
        },
        # === IV1:負薪資漏成 5xx ===
        {
            "input": {"salary": -1, "dependents": 0},
            "__target_invariant": "IV1_no_5xx",
            "__hypothesis": (
                "負薪資 service raise ValueError;endpoint 沒包 try/except → 漏成 500。"
                "這是 真 bug"
            ),
        },
        # === IV3:Infinity 字串 ===
        {
            "input": {"salary": "Infinity", "dependents": 0},
            "__target_invariant": "IV3_nan_inf_4xx",
            "__hypothesis": "+Infinity by-design 走 cap-to-max → 200 OK,IV3 不 fire",
        },
        # === IV7:缺 salary ===
        {
            "input": {"salary": None, "dependents": 0},
            "__target_invariant": "IV7_missing_salary_4xx",
            "__hypothesis": "缺 salary,FastAPI Query(...) 應回 422",
        },
        # === IV5:200 響應的 finite 檢查(對照組) ===
        {
            "input": {"salary": 100000, "dependents": 3},
            "__target_invariant": "IV5_finite_premiums",
            "__hypothesis": "正常輸入,所有保費應為有限數",
        },
        # === IV6:超大 salary(走 cap),保費應有限正 ===
        {
            "input": {"salary": 1e18, "dependents": 0},
            "__target_invariant": "IV6_nonneg_premiums",
            "__hypothesis": "極大 salary 通過 cap 後保費仍應為非負有限",
        },
        # === negative dependents ===
        {
            "input": {"salary": 30000, "dependents": -5},
            "__target_invariant": "IV1_no_5xx",
            "__hypothesis": "Query(int) 接負數;service 內 dependents 被 clamp 到 0,應 200",
        },
    ]


CASES_BY_TARGET: dict[str, list[dict]] = {
    "leave_policy": _leave_policy_cases(),
    "insurance_service": _insurance_cases(),
    "proration": _proration_cases(),
    "insurance_endpoint": _insurance_endpoint_cases(),
}
