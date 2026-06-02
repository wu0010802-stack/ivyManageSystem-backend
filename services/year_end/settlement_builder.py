"""services/year_end/settlement_builder.py — 年終結算 builder helpers（階段1）。

決策④：節慶=角色基數查表（BonusConfig 最新 is_active 列），非全年加總。

此模組只提供三個純/純ish helper：
  - festival_base_for_role   : 依角色查節慶獎金基數
  - compute_hire_months      : 計算在職月數（整個 cycle 或部分）
  - resolve_org_achievement_rate : 解析組織績效達成率（滿年平均 / 僅在職學期）

build_settlements 編排邏輯在後續 Task 3 實作，本模組不含。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from models.config import BonusConfig

# --------------------------------------------------------------------------- #
# 精度常數                                                                     #
# --------------------------------------------------------------------------- #

_Q2 = Decimal("0.01")  # 金額，小數 2 位
_Q1 = Decimal("0.1")  # 達成率，小數 1 位


def _q2(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _q1(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(_Q1, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# 角色 key → BonusConfig 節慶基數欄位名稱對應表                               #
# --------------------------------------------------------------------------- #

_FESTIVAL_FIELD: dict[str, str] = {
    "head_teacher_ab": "head_teacher_ab",
    "head_teacher_c": "head_teacher_c",
    "assistant_teacher_ab": "assistant_teacher_ab",
    "assistant_teacher_c": "assistant_teacher_c",
    "principal": "principal_festival",
    "director": "director_festival",
    "leader": "leader_festival",
    "driver": "driver_festival",
    "designer": "designer_festival",
    "admin": "admin_festival",
    "art_teacher": "art_teacher_festival",
}


# --------------------------------------------------------------------------- #
# Helper 1：節慶獎金角色基數查表                                               #
# --------------------------------------------------------------------------- #


def festival_base_for_role(db: Session, role_key: str) -> Decimal:
    """查最新 BonusConfig（is_active + id DESC）取角色對應節慶基數。

    Args:
        db: SQLAlchemy session。
        role_key: 角色識別鍵，須存在於 _FESTIVAL_FIELD 對應表中。

    Returns:
        Decimal（小數 2 位）；查無設定或 role_key 不在對應表時回 Decimal("0")。
    """
    field_name = _FESTIVAL_FIELD.get(role_key)
    if field_name is None:
        return Decimal("0")

    config: BonusConfig | None = (
        db.query(BonusConfig)
        .filter(BonusConfig.is_active == True)  # noqa: E712
        .order_by(BonusConfig.id.desc())
        .first()
    )
    if config is None:
        return Decimal("0")

    raw = getattr(config, field_name, None)
    if raw is None:
        return Decimal("0")

    return _q2(raw)


# --------------------------------------------------------------------------- #
# Helper 2：在職月數計算                                                       #
# --------------------------------------------------------------------------- #


def compute_hire_months(emp: Any, cycle_start: date, cycle_end: date) -> Decimal:
    """計算員工在 [cycle_start, cycle_end] 週期內的在職月數。

    月數採 inclusive 計算（start.m 到 end.m 均算一個月）。
    結果 clamp 在 [0, 12]。

    Args:
        emp: 任意有 hire_date / resign_date 屬性的物件（None 表示無限制）。
        cycle_start: 週期開始日。
        cycle_end:   週期結束日。

    Returns:
        Decimal（整數），在職月數。
    """
    hire_date: date | None = getattr(emp, "hire_date", None)
    resign_date: date | None = getattr(emp, "resign_date", None)

    # 將 None 視為「週期邊界」
    effective_start = (
        max(hire_date, cycle_start) if hire_date is not None else cycle_start
    )
    effective_end = (
        min(resign_date, cycle_end) if resign_date is not None else cycle_end
    )

    if effective_end < effective_start:
        return Decimal("0")

    months = (
        (effective_end.year - effective_start.year) * 12
        + (effective_end.month - effective_start.month)
        + 1
    )

    # clamp 0..12
    months = max(0, min(12, months))
    return Decimal(str(months))


# --------------------------------------------------------------------------- #
# Helper 3：組織績效達成率解析                                                 #
# --------------------------------------------------------------------------- #


def resolve_org_achievement_rate(
    first: Any,
    second: Any,
    *,
    worked_first: bool,
    worked_second: bool,
) -> Decimal:
    """解析組織績效達成率。

    - 滿年（worked_first=True, worked_second=True）：兩學期平均（四捨五入小數 1 位）。
    - 只在職一學期：直接取該學期的達成率。
    - 兩者皆 False（異常資料）：回 Decimal("0.0")。

    Args:
        first:         第一學期達成率（數值或 Decimal）。
        second:        第二學期達成率（數值或 Decimal）。
        worked_first:  是否在第一學期在職。
        worked_second: 是否在第二學期在職。

    Returns:
        Decimal（小數 1 位）。
    """
    rates: list[Decimal] = []
    if worked_first:
        rates.append(Decimal(str(first)))
    if worked_second:
        rates.append(Decimal(str(second)))

    if not rates:
        return Decimal("0.0")

    average = sum(rates) / len(rates)
    return _q1(average)
