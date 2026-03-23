"""
請假相關共用 Pydantic 驗證函式。

同時被 api/leaves.py（管理端）與 api/portal/_shared.py（教師入口）使用，
避免相同規則在兩處重複定義。
"""

from datetime import date as _date
from typing import Optional


def validate_leave_hours_value(v: float) -> float:
    """驗證請假時數：最小 0.5 小時、最大 480 小時、必須為 0.5 的倍數。"""
    if v < 0.5:
        raise ValueError("請假時數至少 0.5 小時")
    if v > 480:
        raise ValueError("請假時數不得超過 480 小時")
    if round(v * 2) != v * 2:
        raise ValueError("請假時數必須為 0.5 小時的倍數（如 0.5、1、1.5、2…）")
    return v


def validate_leave_date_order(
    start_date: Optional[_date],
    end_date: Optional[_date],
) -> None:
    """驗證假單日期順序與跨月限制（在 model_validator 內呼叫）。"""
    if start_date and end_date and end_date < start_date:
        raise ValueError("結束日期不得早於開始日期")
    if start_date and end_date and (
        start_date.year != end_date.year
        or start_date.month != end_date.month
    ):
        raise ValueError(
            "請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請"
            f"（本次 {start_date.year}/{start_date.month:02d} 月 →"
            f" {end_date.year}/{end_date.month:02d} 月）"
        )
