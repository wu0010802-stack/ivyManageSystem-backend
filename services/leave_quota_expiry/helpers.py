"""Pure function helpers — 不依賴 session，可獨立測試。"""

from datetime import date


def _next_month(today: date) -> tuple[int, int]:
    """跨年 12→1 wrap。

    Returns:
        (year, month) tuple
    """
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


def _add_one_year_with_feb29_handling(d: date) -> date:
    """2/29 + 1y 落非閏年順延 2/28。

    When adding one year to 2/29 of a leap year and the target year is not
    a leap year, returns 2/28 instead of raising ValueError.
    """
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        # Handles 2/29 → non-leap year case
        return d.replace(year=d.year + 1, day=28)


def _resolve_hourly_wage(emp, ref_date: date) -> float:
    """月薪/30/8 或 hourly_rate。

    For hourly employees, returns hourly_rate.
    For monthly employees, returns base_salary / 30 / 8.

    Args:
        emp: Employee object with employee_type and either hourly_rate or base_salary
        ref_date: Reference date (future-proof for EmployeeSalaryHistory lookup)

    Returns:
        Hourly wage as float. Returns 0.0 if employee_type is unknown or salary is ≤ 0.

    註：未來若引入 EmployeeSalaryHistory 取 ref_date 當下生效薪資，
    在此 helper 內展開即可，scheduler caller 不需改。
    """
    if emp.employee_type == "hourly":
        return float(emp.hourly_rate or 0)

    monthly = float(emp.base_salary or 0)
    if monthly <= 0:
        return 0.0

    return monthly / 30 / 8
