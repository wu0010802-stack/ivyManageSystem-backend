"""appraisal → year_end 橋接 service。

將學期制考核（AppraisalSummary.bonus_amount）寫入既有 special_bonus_items 表的
APPRAISAL_HALF_BONUS_FIRST/SECOND slot，供 salary engine 2 月 calculate 時 pull。

業務規則：
- payout 發放於 civil_year N 的 2/5
- 含「上學年下學期 (N-1.下)」+「本學年上學期 (N.上)」兩筆
- target year_end_cycles.academic_year = N - 1911 - 1（本學年，民國）
- bonus_type 對 period_label 的 mapping：
    FIRST  = 較早 = N-1.下 → period_label = f"{N-1-1911}下"
    SECOND = 較晚 = N.上   → period_label = f"{N-1911-1}上"
  ⚠️ SpecialBonusType 的 FIRST/SECOND 與 AppraisalCycle.Semester.FIRST/SECOND 反向（前者時間順序、後者學期上下）。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    Semester,
    SummaryStatus,
)
from models.employee import Employee
from models.year_end import SpecialBonusType


def civil_year_to_target_academic_year(civil_year: int) -> int:
    """payout 發放國曆年 N → 對應本學年（民國）。

    2026 國曆年 2/5 = 114 學年下學期初（學年 8 月起算），所以 target = 114。
    """
    return civil_year - 1911 - 1


def map_bonus_type_to_period_label(
    bonus_type: SpecialBonusType, target_academic_year: int
) -> str:
    """FIRST → 前一學年下學期；SECOND → 本學年上學期。"""
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST:
        return f"{target_academic_year - 1}下"
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND:
        return f"{target_academic_year}上"
    raise ValueError(
        f"map_bonus_type_to_period_label 僅支援 APPRAISAL_HALF_BONUS_*；got {bonus_type}"
    )


@dataclass
class PayoutPreviewRow:
    employee_id: int
    employee_name: str
    role_group: str
    earlier_summary_id: Optional[int]
    earlier_amount: Decimal
    earlier_cycle_finalized: bool
    later_summary_id: Optional[int]
    later_amount: Decimal
    later_cycle_finalized: bool
    total_amount: Decimal
    is_inactive: bool
    warnings: list[str] = field(default_factory=list)


def resolve_target_cycles(
    db: Session, payout_year: int
) -> tuple[AppraisalCycle, AppraisalCycle]:
    """payout_year (civil 2026) → (earlier_cycle 113下, later_cycle 114上)。"""
    target_academic_year = civil_year_to_target_academic_year(payout_year)
    earlier = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == target_academic_year - 1,
            AppraisalCycle.semester == Semester.SECOND,
        )
    )
    if earlier is None:
        raise LookupError(
            f"appraisal_cycle academic_year={target_academic_year - 1} SECOND 不存在；"
            "請先在考核管理建立此 cycle"
        )
    later = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == target_academic_year,
            AppraisalCycle.semester == Semester.FIRST,
        )
    )
    if later is None:
        raise LookupError(
            f"appraisal_cycle academic_year={target_academic_year} FIRST 不存在；"
            "請先在考核管理建立此 cycle"
        )
    return earlier, later


def _cycle_is_finalized(cycle: AppraisalCycle) -> bool:
    """CycleStatus.CLOSED = 已封存 = finalized。"""
    status = cycle.status
    status_name = getattr(status, "name", str(status))
    return status_name == "CLOSED"


def preview_payout(db: Session, payout_year: int) -> list[PayoutPreviewRow]:
    """為兩個 cycle 的 participants 算金額 snapshot，回傳 row 列表。

    - is_excluded=True 不列出
    - 員工只在一個 cycle 出現：另一筆 0 + warning
    - 兩 cycle 都未參與：不列出
    """
    earlier, later = resolve_target_cycles(db, payout_year)

    def _fetch(
        cycle: AppraisalCycle,
    ) -> dict[int, tuple[AppraisalParticipant, Optional[AppraisalSummary]]]:
        rows = db.execute(
            select(AppraisalParticipant, AppraisalSummary)
            .outerjoin(
                AppraisalSummary,
                AppraisalSummary.participant_id == AppraisalParticipant.id,
            )
            .where(
                AppraisalParticipant.cycle_id == cycle.id,
                AppraisalParticipant.is_excluded.is_(False),
            )
        ).all()
        return {p.employee_id: (p, s) for p, s in rows}

    earlier_map = _fetch(earlier)
    later_map = _fetch(later)
    earlier_finalized = _cycle_is_finalized(earlier)
    later_finalized = _cycle_is_finalized(later)

    all_emp_ids: OrderedDict[int, None] = OrderedDict()
    for eid in list(earlier_map.keys()) + list(later_map.keys()):
        all_emp_ids[eid] = None

    employees: dict[int, Employee] = (
        {
            e.id: e
            for e in db.scalars(
                select(Employee).where(Employee.id.in_(all_emp_ids.keys()))
            ).all()
        }
        if all_emp_ids
        else {}
    )

    result: list[PayoutPreviewRow] = []
    for emp_id in all_emp_ids:
        emp = employees.get(emp_id)
        if emp is None:
            continue
        earlier_entry = earlier_map.get(emp_id)
        later_entry = later_map.get(emp_id)
        earlier_participant, earlier_summary = (
            earlier_entry if earlier_entry else (None, None)
        )
        later_participant, later_summary = later_entry if later_entry else (None, None)

        e_amount = (
            Decimal(earlier_summary.bonus_amount) if earlier_summary else Decimal(0)
        )
        l_amount = Decimal(later_summary.bonus_amount) if later_summary else Decimal(0)

        warnings: list[str] = []
        if emp_id not in earlier_map:
            warnings.append("not_participated_in_earlier")
        if emp_id not in later_map:
            warnings.append("not_participated_in_later")
        if earlier_summary and earlier_summary.status != SummaryStatus.FINALIZED:
            warnings.append("earlier_summary_not_finalized")
        if later_summary and later_summary.status != SummaryStatus.FINALIZED:
            warnings.append("later_summary_not_finalized")

        active_participant = earlier_participant or later_participant
        assert (
            active_participant is not None
        )  # guaranteed: emp_id is in at least one map
        role_group_obj = active_participant.role_group
        role_group_str = getattr(role_group_obj, "value", str(role_group_obj))

        result.append(
            PayoutPreviewRow(
                employee_id=emp_id,
                employee_name=emp.name,
                role_group=role_group_str,
                earlier_summary_id=(earlier_summary.id if earlier_summary else None),
                earlier_amount=e_amount,
                earlier_cycle_finalized=earlier_finalized,
                later_summary_id=(later_summary.id if later_summary else None),
                later_amount=l_amount,
                later_cycle_finalized=later_finalized,
                total_amount=e_amount + l_amount,
                is_inactive=not bool(emp.is_active),
                warnings=warnings,
            )
        )
    return result
