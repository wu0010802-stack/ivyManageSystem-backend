"""
共用學年度計算工具。
"""

from datetime import date
from typing import Optional

from fastapi import HTTPException


def resolve_current_academic_term(
    target_date: Optional[date] = None,
) -> tuple[int, int]:
    """根據日期決定當前學年度與學期（學年度以民國年表示）。

    - 8月以後 → 當年上學期（semester=1）
    - 2月~7月 → 前一年下學期（semester=2）
    - 1月 → 前一年上學期（semester=1）
    """
    target_date = target_date or date.today()
    if target_date.month >= 8:
        return target_date.year - 1911, 1
    if target_date.month >= 2:
        return target_date.year - 1 - 1911, 2
    return target_date.year - 1 - 1911, 1


def resolve_academic_term_filters(
    school_year: Optional[int],
    semester: Optional[int],
) -> tuple[int, int]:
    """解析學期篩選參數，未提供時自動使用當前學期；只提供一個時拋出 400。"""
    if school_year is None and semester is None:
        return resolve_current_academic_term()
    if school_year is None or semester is None:
        raise HTTPException(
            status_code=400, detail="school_year 與 semester 需同時提供"
        )
    return school_year, semester


def semester_int_to_enum(sem_int: int):
    """將 1/2 整數轉為 models.appraisal.Semester enum（FIRST/SECOND）。

    考核系統用 Semester enum，但其他模組（班級、活動報名）用 int(1/2)；
    此 helper 集中轉換，避免各 caller 自寫。
    """
    from models.appraisal import Semester

    if sem_int == 1:
        return Semester.FIRST
    if sem_int == 2:
        return Semester.SECOND
    raise ValueError(f"semester must be 1 or 2, got {sem_int}")


def semester_enum_to_int(sem) -> int:
    """將 models.appraisal.Semester enum 轉為 1/2 整數。

    用於跨模組查詢（如 ActivityRegistration.semester 是 int）。
    """
    from models.appraisal import Semester

    if sem == Semester.FIRST or sem == "FIRST":
        return 1
    if sem == Semester.SECOND or sem == "SECOND":
        return 2
    raise ValueError(f"semester must be Semester enum, got {sem!r}")
