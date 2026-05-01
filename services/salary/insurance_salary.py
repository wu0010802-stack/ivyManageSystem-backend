"""投保薪資合規驗證（勞保條例第 14 條 / 勞退條例第 14 條）

「投保薪資不得低於實際工資」。違反時勞保局可處差額 2-4 倍罰鍰（勞保條例第 72 條）。

- 月薪制：insurance_salary_level 非 0 時 ≥ base_salary
- 時薪制：insurance_salary_level 非 0 時 ≥ hourly_rate × 176（估算月工時）
- 任何員工：非 0 時不得低於基本工資
"""

from fastapi import HTTPException

from .minimum_wage import MINIMUM_MONTHLY_WAGE

ESTIMATED_MONTHLY_HOURS = 176  # 22 工作日 × 8h，時薪制估算月工時基準


def resolve_insurance_salary_raw(
    employee_type: str,
    base_salary: float,
    insurance_salary_level: float,
    hourly_rate: float,
) -> float:
    """決定用來查投保級距的 raw 薪資值，永遠取「合法下限」：

    - 月薪制：max(insurance_salary_level, base_salary)
    - 時薪制：max(insurance_salary_level, hourly_rate × ESTIMATED_MONTHLY_HOURS)

    即使 DB 的 insurance_salary_level 短報（< 實際工資），薪資計算也會用
    max 取回合規值，避免系統自動產出違反勞保條例第 14 條的短報保費。
    高報（insurance > 工資，對員工有利）則保留 insurance。
    """
    ins = float(insurance_salary_level or 0)
    if employee_type == "regular":
        base = float(base_salary or 0)
        return max(ins, base)
    if employee_type == "hourly":
        rate = float(hourly_rate or 0)
        estimated = rate * ESTIMATED_MONTHLY_HOURS if rate > 0 else 0.0
        return max(ins, estimated)
    return ins


def validate_insurance_salary(
    employee_type: str,
    base_salary: float,
    insurance_salary_level: float,
    hourly_rate: float = 0,
) -> None:
    """驗證投保薪資不低於實際工資。

    insurance_salary_level 為 0 或 None 表示未設定，系統會 fallback 至 base_salary，允許。
    """
    ins = float(insurance_salary_level or 0)
    if ins <= 0:
        return

    if ins < MINIMUM_MONTHLY_WAGE:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INSURANCE_BELOW_BASE",
                "message": (
                    f"投保薪資 NT${ins:.0f} 低於法定基本工資 NT${MINIMUM_MONTHLY_WAGE:.0f}"
                    "（勞保條例第 14 條）"
                ),
                "context": {
                    "kind": "below_minimum_wage",
                    "base": MINIMUM_MONTHLY_WAGE,
                    "current": ins,
                    "suggested": MINIMUM_MONTHLY_WAGE,
                },
            },
        )

    if employee_type == "regular":
        base = float(base_salary or 0)
        if base > 0 and ins < base:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INSURANCE_BELOW_BASE",
                    "message": (
                        f"投保薪資 NT${ins:.0f} 低於月薪 NT${base:.0f}，"
                        "違反勞保條例第 14 條（投保薪資不得低於實際工資）；"
                        "勞保局查核可處短繳差額 2-4 倍罰鍰"
                    ),
                    "context": {
                        "kind": "below_monthly_wage",
                        "base": base,
                        "current": ins,
                        "suggested": base,
                    },
                },
            )
    elif employee_type == "hourly":
        rate = float(hourly_rate or 0)
        if rate > 0:
            est_monthly = rate * ESTIMATED_MONTHLY_HOURS
            if ins < est_monthly:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INSURANCE_BELOW_BASE",
                        "message": (
                            f"投保薪資 NT${ins:.0f} 低於估算月工資 NT${est_monthly:.0f}"
                            f"（時薪 NT${rate:.0f} × {ESTIMATED_MONTHLY_HOURS} 小時），"
                            "違反勞保條例第 14 條"
                        ),
                        "context": {
                            "kind": "below_hourly_estimated",
                            "base": est_monthly,
                            "current": ins,
                            "suggested": est_monthly,
                        },
                    },
                )
