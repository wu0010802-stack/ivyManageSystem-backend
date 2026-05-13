"""費用退費計算 — 純函式 helpers

依教育局規定:
- 註冊費/雜費 三段比例:T_served/T_total <1/3 退 2/3、1/3~2/3 退 1/3、≥2/3 不退
- 月費:事先請假連續 ≥5 上課日 → 按比例退「餐點+交通」(看 breakdown);無 breakdown fallback 全額按比例
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from services.workday_rules import classify_day


def calc_enrollment_refund(*, amount_due: int, T_total: int, T_served: int) -> dict:
    """計算註冊費/雜費建議退費金額。

    Args:
        amount_due: 該筆費用原始金額(整數,單位元)
        T_total: 教保服務總日數(>0)
        T_served: 學生已服務日數

    Returns:
        {"suggested_amount": int, "calc_method": "enrollment_ratio",
         "calc_payload": {...}, "warnings": [...]}
    """
    if T_total <= 0:
        raise ValueError("T_total 必須 > 0")
    if T_served < 0:
        T_served = 0
    if T_served > T_total:
        T_served = T_total

    ratio = T_served / T_total
    if ratio < 1 / 3:
        refund_ratio_label = "2/3"
        refund_amount = round(amount_due * 2 / 3)
        ratio_band = "<1/3"
    elif ratio < 2 / 3:
        refund_ratio_label = "1/3"
        refund_amount = round(amount_due * 1 / 3)
        ratio_band = "1/3..2/3"
    else:
        refund_ratio_label = "0"
        refund_amount = 0
        ratio_band = ">=2/3"

    return {
        "suggested_amount": refund_amount,
        "calc_method": "enrollment_ratio",
        "calc_payload": {
            "T_total": T_total,
            "T_served": T_served,
            "served_ratio": round(ratio, 4),
            "ratio": ratio_band,
            "refund_ratio": refund_ratio_label,
            "amount_due": amount_due,
            "formula": f"{amount_due} × {refund_ratio_label} = {refund_amount}",
        },
        "warnings": [],
    }


def longest_consecutive_workdays(
    leave_days: list[date],
    holiday_map: dict[date, str],
    makeup_map: dict[date, str],
) -> int:
    """從請假日期列表中,計算「最長連續上課日(workday)」段。

    連續判定:日期相鄰(差 1 天),工作日(非週末非假日,或補班日)才計入計數;
    週末/假日不打斷連續性,只是不計入工作日數。
    """
    if not leave_days:
        return 0
    days = sorted(set(leave_days))
    best = 0
    current = 0
    prev = None
    for d in days:
        if prev is not None and (d - prev).days > 1:
            # 區段中斷
            best = max(best, current)
            current = 0
        info = classify_day(d, holiday_map, makeup_map)
        if info["kind"] == "workday":
            current += 1
        prev = d
    best = max(best, current)
    return best


def calc_monthly_refund(
    *,
    amount_due: int,
    breakdown: Optional[dict],
    L_consecutive: int,
    work_days_in_month: int,
    advance_filed: bool,
) -> dict:
    """計算月費建議退費金額。

    Args:
        amount_due: 該月應繳月費金額
        breakdown: 月費組成 dict(tuition/meal/transport),允許 None
        L_consecutive: 該月最長連續請假上課日數
        work_days_in_month: 該月總工作日(分母)
        advance_filed: 是否事先請假(申請日 < 開始日)

    退費條件:必須事先請假 AND L_consecutive >= 5 AND work_days_in_month > 0
    退費金額:(meal + transport) × L / work_days_in_month
    無 breakdown:fallback amount_due × L / work_days_in_month + warning
    """
    warnings: list[str] = []

    if not advance_filed:
        return {
            "suggested_amount": 0,
            "calc_method": "monthly_partial",
            "calc_payload": {
                "L_consecutive": L_consecutive,
                "work_days_in_month": work_days_in_month,
                "advance_filed": False,
                "refundable_components": [],
                "amount_due": amount_due,
                "formula": "未事先請假,不退",
            },
            "warnings": ["未事先請假,依規定不予退費"],
        }

    if L_consecutive < 5:
        return {
            "suggested_amount": 0,
            "calc_method": "monthly_partial",
            "calc_payload": {
                "L_consecutive": L_consecutive,
                "work_days_in_month": work_days_in_month,
                "advance_filed": True,
                "refundable_components": [],
                "amount_due": amount_due,
                "formula": "連續未達 5 上課日,不退",
            },
            "warnings": [f"連續請假 {L_consecutive} 日,未達 5 個上課日門檻"],
        }

    if work_days_in_month <= 0:
        return {
            "suggested_amount": 0,
            "calc_method": "monthly_partial",
            "calc_payload": {
                "L_consecutive": L_consecutive,
                "work_days_in_month": work_days_in_month,
                "advance_filed": True,
                "refundable_components": [],
                "amount_due": amount_due,
                "formula": "該月無工作日,無法計算比例",
            },
            "warnings": ["該月無工作日,退費比例無法計算"],
        }

    proportion = L_consecutive / work_days_in_month

    if breakdown:
        meal = int(breakdown.get("meal", 0) or 0)
        transport = int(breakdown.get("transport", 0) or 0)
        refundable_base = meal + transport
        refund_amount = round(refundable_base * proportion)
        refundable_components = [k for k in ("meal", "transport") if breakdown.get(k)]
        formula = f"(meal {meal} + transport {transport}) × {L_consecutive}/{work_days_in_month} = {refund_amount}"
    else:
        refundable_base = amount_due
        refund_amount = round(amount_due * proportion)
        refundable_components = []
        formula = f"{amount_due} × {L_consecutive}/{work_days_in_month} = {refund_amount} (fallback)"
        warnings.append("無 breakdown,改按全額月費比例退,建議補設範本組成")

    return {
        "suggested_amount": refund_amount,
        "calc_method": "monthly_partial",
        "calc_payload": {
            "L_consecutive": L_consecutive,
            "work_days_in_month": work_days_in_month,
            "advance_filed": True,
            "refundable_components": refundable_components,
            "refundable_base": refundable_base,
            "proportion": round(proportion, 4),
            "breakdown_used": breakdown,
            "amount_due": amount_due,
            "formula": formula,
        },
        "warnings": warnings,
    }
