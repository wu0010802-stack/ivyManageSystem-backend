"""才藝退費計算 — 純函式 helpers。

對齊 services/finance/fee_refund_calculator.py 的回傳形狀：
- 課程：教育局學費三段比例（按已出席堂數），T_served=0 特例全退
- 用品：一律不退（已交付）

純函式不碰 DB；caller 由 services/activity_refund_query.py 預先 query
attendance + course.sessions 後餵入。
"""

from __future__ import annotations

from utils.rounding import round_half_up


def calc_course_refund(*, amount_due: int, T_total: int, T_served: int) -> dict:
    """計算才藝課程退費建議金額（按已出席堂數三段比例）。

    Args:
        amount_due: 課程原始金額（price_snapshot，整數元）
        T_total: 課程總堂數（ActivityCourse.sessions），必須 > 0
        T_served: 學生已出席堂數（is_present=True 的 ActivityAttendance count）

    Raises:
        ValueError: T_total <= 0

    Returns:
        {
          "suggested_amount": int,
          "calc_method": "activity_course_ratio",
          "calc_payload": {
            "T_total": int,
            "T_served": int,                # clamp 後值
            "served_ratio": float,
            "ratio_band": "not_started" | "<1/3" | "1/3..2/3" | ">=2/3",
            "refund_ratio": "1" | "2/3" | "1/3" | "0",
            "amount_due": int,
            "formula": str,
          },
          "warnings": list[str],
        }

    規則:
      T_served == 0          → 全退（not_started 特例，業界慣例「未開課全退」）
      0 < ratio < 1/3        → 退 2/3
      1/3 ≤ ratio < 2/3      → 退 1/3
      ratio ≥ 2/3            → 0
      T_served < 0           → clamp 0
      T_served > T_total     → clamp T_total
    """
    if T_total <= 0:
        raise ValueError("T_total 必須 > 0")
    if T_served < 0:
        T_served = 0
    if T_served > T_total:
        T_served = T_total

    if T_served == 0:
        suggested = amount_due
        ratio_band = "not_started"
        refund_ratio_label = "1"
        formula = f"未開課全退：{amount_due}"
        ratio = 0.0
    else:
        ratio = T_served / T_total
        if ratio < 1 / 3:
            ratio_band = "<1/3"
            refund_ratio_label = "2/3"
            suggested = round_half_up(amount_due * 2 / 3)
        elif ratio < 2 / 3:
            ratio_band = "1/3..2/3"
            refund_ratio_label = "1/3"
            suggested = round_half_up(amount_due * 1 / 3)
        else:
            ratio_band = ">=2/3"
            refund_ratio_label = "0"
            suggested = 0
        formula = f"{amount_due} × {refund_ratio_label} = {suggested}"

    return {
        "suggested_amount": suggested,
        "calc_method": "activity_course_ratio",
        "calc_payload": {
            "T_total": T_total,
            "T_served": T_served,
            "served_ratio": round_half_up(ratio, 4),
            "ratio_band": ratio_band,
            "refund_ratio": refund_ratio_label,
            "amount_due": amount_due,
            "formula": formula,
        },
        "warnings": [],
    }


def calc_supply_refund(*, amount_due: int) -> dict:
    """用品（教材）退費 — 一律不退（已交付）。

    Returns:
        suggested_amount=0, calc_method="activity_supply_no_refund",
        warnings=["用品（教材）已交付，不予退費"]
    """
    return {
        "suggested_amount": 0,
        "calc_method": "activity_supply_no_refund",
        "calc_payload": {
            "amount_due": amount_due,
            "formula": "用品一律不退（已交付）",
        },
        "warnings": ["用品（教材）已交付，不予退費"],
    }
