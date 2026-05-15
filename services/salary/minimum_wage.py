"""法定基本工資（勞基法第 21 條）合規驗證。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)

MINIMUM_MONTHLY_WAGE = 29500
MINIMUM_HOURLY_WAGE = 196


def get_minimum_wage(at_date: date) -> Tuple[int, int]:
    """取指定日期適用之基本工資 (monthly, hourly)。"""
    del at_date
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
