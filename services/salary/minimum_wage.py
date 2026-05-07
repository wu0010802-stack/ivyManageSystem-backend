"""法定基本工資（勞基法第 21 條）合規驗證。

> ⚠ 自 2026-05 起改為查 `minimum_wage_history` 表。
> 排程同步：services/gov_data + admin 在 /admin/gov-data-sync 套用。
> 兩個常數 MINIMUM_MONTHLY_WAGE / MINIMUM_HOURLY_WAGE 保留作為 DB 失靈時的 fallback。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Fallback 常數（DB 查詢失敗時回退用；正式取值請呼叫 get_minimum_wage）
MINIMUM_MONTHLY_WAGE = 29500
MINIMUM_HOURLY_WAGE = 196


def _query_history(at_date: date) -> Tuple[int, int]:
    """查 minimum_wage_history 取「effective_date ≤ at_date 中最新一筆」。

    可被測試 monkeypatch（模擬 DB down）。
    """
    from models.database import MinimumWageHistory, session_scope

    with session_scope() as s:
        row = (
            s.query(MinimumWageHistory)
            .filter(MinimumWageHistory.effective_date <= at_date)
            .order_by(MinimumWageHistory.effective_date.desc())
            .first()
        )
        if row is None:
            raise LookupError(f"minimum_wage_history empty at {at_date}")
        return row.monthly, row.hourly


def get_minimum_wage(at_date: date) -> Tuple[int, int]:
    """取指定日期適用之基本工資 (monthly, hourly)。

    DB 查詢失敗時 log warning + 回退常數，避免薪資模組整個掛掉。
    """
    try:
        return _query_history(at_date)
    except Exception as exc:
        logger.warning(
            "minimum_wage_history 查詢失敗 (%s)，fallback 常數 monthly=%d hourly=%d",
            exc,
            MINIMUM_MONTHLY_WAGE,
            MINIMUM_HOURLY_WAGE,
        )
        return MINIMUM_MONTHLY_WAGE, MINIMUM_HOURLY_WAGE


def validate_minimum_wage(
    employee_type: str, base_salary: float, hourly_rate: float
) -> None:
    """驗證員工薪資不低於今日法定基本工資。

    底薪/時薪為 0 視為尚未設定，不檢查；非 0 且低於法定值時拋 400。
    """
    monthly, hourly = get_minimum_wage(date.today())
    if employee_type == "regular":
        if base_salary and base_salary < monthly:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BELOW_MINIMUM_WAGE",
                    "message": (
                        f"月薪 NT${base_salary:.0f} 低於法定基本工資 "
                        f"NT${monthly}（勞基法第 21 條）"
                    ),
                    "context": {
                        "employee_type": "regular",
                        "minimum": monthly,
                        "current": base_salary,
                    },
                },
            )
    elif employee_type == "hourly":
        if hourly_rate and hourly_rate < hourly:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BELOW_MINIMUM_WAGE",
                    "message": (
                        f"時薪 NT${hourly_rate:.0f} 低於法定基本工資 "
                        f"NT${hourly}（勞基法第 21 條）"
                    ),
                    "context": {
                        "employee_type": "hourly",
                        "minimum": hourly,
                        "current": hourly_rate,
                    },
                },
            )
