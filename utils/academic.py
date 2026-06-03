"""共用學年度計算工具。

- resolve_current_academic_term(target_date=None, session=None):
    優先查 AcademicTerm.is_current=true；找不到 fallback 到日期推算 + warning log。
    target_date 顯式傳值時跳過 DB 查詢（用於測試/歷史查詢）。
- default_current_academic_term_for_column():
    SQLAlchemy Column.default 專用、純日期推算、不查 DB。
- resolve_academic_term_filters(school_year, semester, session=None):
    既有介面，多接 session 可選。
- _resolve_by_date(target_date): 私有純函式，原本日期推算邏輯。
- semester_int_to_enum / semester_enum_to_int: 不動。
"""

import logging
from datetime import date
from utils.taipei_time import today_taipei
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _resolve_by_date(target_date: date) -> tuple[int, int]:
    """純日期推算學年/學期（民國年）。

    - 8月以後 → 當年上學期（semester=1）
    - 2月~7月 → 前一年下學期（semester=2）
    - 1月 → 前一年上學期（semester=1）
    """
    if target_date.month >= 8:
        return target_date.year - 1911, 1
    if target_date.month >= 2:
        return target_date.year - 1 - 1911, 2
    return target_date.year - 1 - 1911, 1


def term_bounds(school_year: int, semester: int) -> tuple[date, date]:
    """由民國學年 + 學期回推固定起訖日（學期日期是日期的純函數，無需設定）。

    - 上學期（semester=1）：8/1 ~ 隔年 1/31
    - 下學期（semester=2）：2/1 ~ 同年 7/31
    西元 = 民國學年 + 1911。
    """
    base = school_year + 1911
    if semester == 1:
        return date(base, 8, 1), date(base + 1, 1, 31)
    if semester == 2:
        return date(base + 1, 2, 1), date(base + 1, 7, 31)
    raise ValueError(f"semester must be 1 or 2, got {semester}")


def resolve_current_academic_term(
    target_date: Optional[date] = None,
    session: Optional[Session] = None,
) -> tuple[int, int]:
    """決定當前學年/學期（民國年）。

    學期是「今天日期」的純函數（上學期 8/1–隔年1/31、下學期 2/1–7/31），
    不再讀 AcademicTerm.is_current；is_current 僅供 turnover 排程器當結轉標記。
    session 參數保留以維持既有呼叫相容，但不再使用。
    """
    return _resolve_by_date(
        target_date if target_date is not None else today_taipei()
    )


def default_current_academic_term_for_column() -> tuple[int, int]:
    """SQLAlchemy Column.default 專用：純日期推算、不查 DB。

    Classroom._default_school_year/_default_semester 在 INSERT 時呼叫，
    這時候不該觸發 DB query（會在已開 session 內套娃）。
    """
    return _resolve_by_date(today_taipei())


def resolve_academic_term_filters(
    school_year: Optional[int],
    semester: Optional[int],
    session: Optional[Session] = None,
) -> tuple[int, int]:
    """解析學期篩選參數，未提供時自動使用當前學期；只提供一個時拋 400。"""
    if school_year is None and semester is None:
        return resolve_current_academic_term(session=session)
    if school_year is None or semester is None:
        raise HTTPException(
            status_code=400, detail="school_year 與 semester 需同時提供"
        )
    return school_year, semester


def semester_int_to_enum(sem_int: int):
    """將 1/2 整數轉為 models.appraisal.Semester enum（FIRST/SECOND）。"""
    from models.appraisal import Semester

    if sem_int == 1:
        return Semester.FIRST
    if sem_int == 2:
        return Semester.SECOND
    raise ValueError(f"semester must be 1 or 2, got {sem_int}")


def semester_enum_to_int(sem) -> int:
    """將 models.appraisal.Semester enum 轉為 1/2 整數。"""
    from models.appraisal import Semester

    if sem == Semester.FIRST or sem == "FIRST":
        return 1
    if sem == Semester.SECOND or sem == "SECOND":
        return 2
    raise ValueError(f"semester must be Semester enum, got {sem!r}")
