"""法定基本工資（勞基法第 21 條）合規驗證

勞動部公告值（每年 1/1 調整）：
- 2026/1/1 起：月薪 NT$29,500、時薪 NT$196
- 2025/1/1 起：月薪 NT$28,590、時薪 NT$190

若政府調升，更新下方常數並同步調整 test_minimum_wage_constants_meet_statutory
中的最低斷言值；測試會在常數回降時失敗。
"""

from fastapi import HTTPException

MINIMUM_MONTHLY_WAGE = 29500.0
MINIMUM_HOURLY_WAGE = 196.0


def validate_minimum_wage(
    employee_type: str, base_salary: float, hourly_rate: float
) -> None:
    """驗證員工薪資設定不低於法定基本工資。

    底薪/時薪為 0 視為尚未設定，不檢查；非 0 且低於法定值時拋 400。
    """
    if employee_type == "regular":
        if base_salary and base_salary < MINIMUM_MONTHLY_WAGE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"月薪 NT${base_salary:.0f} 低於法定基本工資 "
                    f"NT${MINIMUM_MONTHLY_WAGE:.0f}（勞基法第 21 條）"
                ),
            )
    elif employee_type == "hourly":
        if hourly_rate and hourly_rate < MINIMUM_HOURLY_WAGE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"時薪 NT${hourly_rate:.0f} 低於法定基本工資 "
                    f"NT${MINIMUM_HOURLY_WAGE:.0f}（勞基法第 21 條）"
                ),
            )
