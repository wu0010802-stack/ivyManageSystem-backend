"""學期日期工具 + 職稱推薦 role_group。

民國年制：第一學期 8/1 ~ 翌年 1/31；第二學期 2/1 ~ 7/31。
base_score_calc_date 預設 9/15（上）/ 3/15（下）。
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from models.appraisal import RoleGroup, Semester

from .constants import (
    COOK_KEYWORDS,
    HEAD_TEACHER_KEYWORDS,
    STAFF_KEYWORDS,
    SUPERVISOR_KEYWORDS,
)


def default_cycle_dates(academic_year: int, semester) -> tuple[date, date, date]:
    """回傳 (start_date, end_date, base_score_calc_date)。

    academic_year 為民國年（114 = 2025 西元）。
    """
    gregorian = academic_year + 1911
    sem_value = semester.value if isinstance(semester, Semester) else semester
    if sem_value == "FIRST":
        return (
            date(gregorian, 8, 1),
            date(gregorian + 1, 1, 31),
            date(gregorian, 9, 15),
        )
    return (
        date(gregorian + 1, 2, 1),
        date(gregorian + 1, 7, 31),
        date(gregorian + 1, 3, 15),
    )


def default_calc_date(semester, year_gregorian: int) -> date:
    sem_value = semester.value if isinstance(semester, Semester) else semester
    if sem_value == "FIRST":
        return date(year_gregorian, 9, 15)
    return date(year_gregorian, 3, 15)


def _kw_not_after_fu(title: str, kw: str) -> bool:
    """kw 是否出現在 title 中且前一字非「副」（避免「副班導師」誤判為班導）。"""
    idx = title.find(kw)
    while idx != -1:
        if idx == 0 or title[idx - 1] != "副":
            return True
        idx = title.find(kw, idx + 1)
    return False


def suggest_role_group(title: Optional[str]) -> RoleGroup:
    """依職稱字串推薦 role_group。空字串 / 未匹配 → ASSISTANT（保守預設）。

    匹配順序：SUPERVISOR > COOK > STAFF > HEAD_TEACHER > ASSISTANT
    （COOK / STAFF 比 HEAD_TEACHER 前，因「廚房助理」等也可能含「助」字混淆）。
    """
    if not title:
        return RoleGroup.ASSISTANT
    for kw in SUPERVISOR_KEYWORDS:
        if kw in title:
            return RoleGroup.SUPERVISOR
    for kw in COOK_KEYWORDS:
        if kw in title:
            return RoleGroup.COOK
    for kw in STAFF_KEYWORDS:
        if kw in title:
            return RoleGroup.STAFF
    for kw in HEAD_TEACHER_KEYWORDS:
        if _kw_not_after_fu(title, kw):
            return RoleGroup.HEAD_TEACHER
    return RoleGroup.ASSISTANT
