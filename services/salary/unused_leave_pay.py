"""未休特休折算工資（勞基法第 38 條第 4 項）

「勞工之特別休假，因年度終結或契約終止而未休之日數，雇主應發給工資」。
離職當月或年度終結時結算；折算公式：未休時數 × 時薪。

時薪由呼叫端依員工類型決定：
- 月薪制：月薪 ÷ 30 ÷ 8
- 時薪制：直接使用 hourly_rate
"""


def calculate_unused_annual_leave_hours(
    entitled_hours: float, used_hours: float
) -> float:
    """未休特休時數 = 應得時數 − 已核准使用時數，下界為 0。"""
    return max(0.0, float(entitled_hours or 0) - float(used_hours or 0))


def calculate_unused_leave_compensation(
    unused_hours: float, hourly_wage: float
) -> float:
    """折算工資 = 未休時數 × 時薪。"""
    if unused_hours is None or unused_hours <= 0:
        return 0.0
    if hourly_wage is None or hourly_wage <= 0:
        return 0.0
    return unused_hours * hourly_wage
