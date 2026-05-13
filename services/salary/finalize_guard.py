"""跨月份/單日封存守衛 — leaves/overtimes/punch_corrections 共用。

Why this module: 原本 `_check_salary_months_not_finalized` 與 `_collect_leave_months`
僅在 api/leaves.py 內，overtimes/punch_corrections 各自重寫單月判斷。
抽到此處後三個 router 統一呼叫，確保新增封存行為一致同步。
"""

from datetime import date
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import and_, or_

from models.database import SalaryRecord


def collect_months_from_range(start_date: date, end_date: date) -> set[tuple[int, int]]:
    """收集 [start_date, end_date] 跨越的所有 (year, month)。用於 leave 跨月假單。"""
    months: set[tuple[int, int]] = set()
    current = start_date.replace(day=1)
    end_first = end_date.replace(day=1)
    while current <= end_first:
        months.add((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def collect_months_from_dates(dates: Iterable[date]) -> set[tuple[int, int]]:
    """收集多個單日所屬的 (year, month)。用於 overtime / punch_correction。"""
    return {(d.year, d.month) for d in dates}


def assert_months_not_finalized(
    session, *, employee_id: int, months: set[tuple[int, int]]
) -> None:
    """commit 前的封存保護守衛。任一月份已封存即 raise HTTPException(409)。"""
    if not months:
        return
    record = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.is_finalized == True,
            or_(
                *(
                    and_(
                        SalaryRecord.salary_year == yr, SalaryRecord.salary_month == mo
                    )
                    for yr, mo in months
                )
            ),
        )
        .first()
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{record.salary_year} 年 {record.salary_month} 月薪資已封存（結算人：{by}），"
                "無法修改該月份的記錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )
