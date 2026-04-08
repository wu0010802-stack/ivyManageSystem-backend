"""
共用學年度計算工具。
"""
from datetime import date
from typing import Optional

from fastapi import HTTPException


def resolve_current_academic_term(target_date: Optional[date] = None) -> tuple[int, int]:
    """根據日期決定當前學年度與學期（學年度以西元年表示）。

    - 8月以後 → 當年上學期（semester=1）
    - 2月~7月 → 前一年下學期（semester=2）
    - 1月 → 前一年上學期（semester=1）
    """
    target_date = target_date or date.today()
    if target_date.month >= 8:
        return target_date.year, 1
    if target_date.month >= 2:
        return target_date.year - 1, 2
    return target_date.year - 1, 1


def resolve_academic_term_filters(
    school_year: Optional[int],
    semester: Optional[int],
) -> tuple[int, int]:
    """解析學期篩選參數，未提供時自動使用當前學期；只提供一個時拋出 400。"""
    if school_year is None and semester is None:
        return resolve_current_academic_term()
    if school_year is None or semester is None:
        raise HTTPException(status_code=400, detail="school_year 與 semester 需同時提供")
    return school_year, semester
