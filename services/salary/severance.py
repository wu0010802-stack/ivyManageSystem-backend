"""資遣費與平均工資計算

法源：
- 平均工資：勞基法第 2 條第 4 款
- 舊制資遣費：勞基法第 17 條
- 新制資遣費：勞工退休金條例第 12 條
"""

from datetime import date


def calculate_service_years(hire_date: date, end_date: date) -> float:
    """年資（小數年）。離職日早於到職日回傳 0。"""
    if end_date <= hire_date:
        return 0.0
    return (end_date - hire_date).days / 365.25


def calculate_average_monthly_wage(records: list[tuple[float, int]]) -> float:
    """平均月工資（勞基法第 2 條第 4 款）。

    records: 事由發生當日前 6 個月，每筆 (該月所得工資, 該月日數)
    公式：工資總額 ÷ 總日數 × 30
    """
    if not records:
        return 0.0
    total_wage = sum(r[0] for r in records)
    total_days = sum(r[1] for r in records)
    if total_days == 0:
        return 0.0
    return total_wage / total_days * 30


def calculate_severance_pay_new(service_years: float, avg_monthly_wage: float) -> float:
    """新制資遣費（勞退條例第 12 條）：每滿 1 年 0.5 個月，上限 6 個月。"""
    if service_years <= 0 or avg_monthly_wage <= 0:
        return 0.0
    months = min(service_years * 0.5, 6.0)
    return avg_monthly_wage * months


def calculate_severance_pay_old(service_years: float, avg_monthly_wage: float) -> float:
    """舊制資遣費（勞基法第 17 條）：每滿 1 年發給 1 個月平均工資，剩餘月數按比例，無上限。"""
    if service_years <= 0 or avg_monthly_wage <= 0:
        return 0.0
    return avg_monthly_wage * service_years
