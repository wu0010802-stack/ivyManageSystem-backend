"""appraisal → year_end 橋接 service。

將學期制考核（AppraisalSummary.bonus_amount）寫入既有 special_bonus_items 表的
APPRAISAL_HALF_BONUS_FIRST/SECOND slot，供 salary engine 2 月 calculate 時 pull。

業務規則：
- payout 發放於 civil_year N 的 2/5
- 含「前一完整學年上學期 (N-1.上)」+「前一完整學年下學期 (N-1.下)」兩筆
- appraisal source academic_year = N - 1911 - 2（前一學年，民國），year_end 容器 academic_year = N - 1911 - 1
- bonus_type 對 period_label 的 mapping：
    FIRST  = 較早 = 前一學年上學期 → period_label = f"{N-1911-2}上"
    SECOND = 較晚 = 前一學年下學期 → period_label = f"{N-1911-2}下"
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    Semester,
    SummaryStatus,
    CycleStatus,
)
from models.employee import Employee
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle

logger = logging.getLogger(__name__)


def civil_year_to_target_academic_year(civil_year: int) -> int:
    """payout 發放國曆年 N → 對應本學年（民國）。

    2026 國曆年 2/5 = 114 學年下學期初（學年 8 月起算），所以 target = 114。
    """
    return civil_year - 1911 - 1


def map_bonus_type_to_period_label(
    bonus_type: SpecialBonusType, target_academic_year: int
) -> str:
    """FIRST → 前一學年上學期；SECOND → 前一學年下學期。"""
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST:
        return f"{target_academic_year - 1}上"
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND:
        return f"{target_academic_year - 1}下"
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
    """payout_year (civil 2026) → (earlier_cycle 113上, later_cycle 113下)。"""
    target_academic_year = civil_year_to_target_academic_year(payout_year)
    source_academic_year = target_academic_year - 1  # 前一完整學年
    earlier = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == source_academic_year,
            AppraisalCycle.semester == Semester.FIRST,
        )
    )
    if earlier is None:
        raise LookupError(
            f"appraisal_cycle academic_year={source_academic_year} FIRST 不存在；"
            "請先在考核管理建立此 cycle"
        )
    later = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == source_academic_year,
            AppraisalCycle.semester == Semester.SECOND,
        )
    )
    if later is None:
        raise LookupError(
            f"appraisal_cycle academic_year={source_academic_year} SECOND 不存在；"
            "請先在考核管理建立此 cycle"
        )
    return earlier, later


def _cycle_is_finalized(cycle: AppraisalCycle) -> bool:
    """CycleStatus.CLOSED = 已封存 = finalized。"""
    return cycle.status == CycleStatus.CLOSED


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


# === Task 4: transactional write — generate_payouts + void_payouts ===


def _is_postgres(db: Session) -> bool:
    """判斷是否連接 PostgreSQL（SQLite 測試環境返回 False）。"""
    return (db.bind is not None) and db.bind.dialect.name == "postgresql"


def _advisory_lock_payout(db: Session, payout_year: int) -> None:
    """transaction-scope advisory lock；避免並行 generate。

    PostgreSQL：pg_advisory_xact_lock（transaction 結束自動釋放）。
    SQLite 測試環境：no-op（單寫入者，無並發）。

    用 md5 計算穩定的 lock key，避免 Python 3.3+ PYTHONHASHSEED 隨機化在
    多 worker 部署下產生不同 hash() 結果導致 advisory lock 失效。
    """
    if not _is_postgres(db):
        logger.debug("_advisory_lock_payout no-op (non-postgres): year=%s", payout_year)
        return
    raw = hashlib.md5(f"aye_payout|{payout_year}".encode()).digest()
    key = int.from_bytes(raw[:8], "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    logger.debug("_advisory_lock_payout acquired: year=%s key=%d", payout_year, key)


@dataclass
class GenerateResult:
    cycle_id: int
    generated_count: int  # 寫入/更新的 SpecialBonusItem 筆數
    affected_employee_count: int
    total_amount: Decimal
    skipped_inactive_count: int  # 過濾掉的 inactive 員工數
    warnings: list[str] = field(default_factory=list)


def _upsert_special_bonus_item(
    db: Session,
    *,
    cycle_id: int,
    employee_id: int,
    bonus_type: SpecialBonusType,
    period_label: str,
    amount: Decimal,
    source_ref: str,
    calc_meta: dict,
    created_by: int,
) -> None:
    """idempotent upsert：先查後寫，相容 SQLite 與 PostgreSQL。

    PostgreSQL 生產環境上層 advisory lock 已防止並發，SELECT-then-INSERT/UPDATE
    可安全使用。若未來需要去掉 advisory lock，可換成 pg_insert ON CONFLICT。
    """
    existing = db.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle_id,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type == bonus_type,
            SpecialBonusItem.period_label == period_label,
        )
    )
    now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    if existing is None:
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle_id,
                employee_id=employee_id,
                bonus_type=bonus_type,
                period_label=period_label,
                amount=amount,
                source_ref=source_ref,
                calc_meta=calc_meta,
                created_by=created_by,
            )
        )
    else:
        existing.amount = amount
        existing.source_ref = source_ref
        existing.calc_meta = calc_meta
        existing.updated_at = now_utc


def generate_payouts(
    db: Session,
    payout_year: int,
    included_inactive_employee_ids: set[int],
    generated_by: int,
) -> GenerateResult:
    """transactional：upsert YearEndCycle + 對每員工兩筆 SpecialBonusItem。

    呼叫端應在 router 用 transactional dep 包；本函式只 flush 不 commit。

    - ACTIVE 員工預設全寫；
    - INACTIVE 員工須在 included_inactive_employee_ids 中才寫。
    - ON CONFLICT（uq_special_bonus_item）→ UPDATE → idempotent。
    - pg_advisory_xact_lock 防並行 generate race（SQLite 環境 no-op）。
    """
    _advisory_lock_payout(db, payout_year)

    target_academic_year = civil_year_to_target_academic_year(payout_year)
    rows = preview_payout(db, payout_year)

    # upsert YearEndCycle（最小 shell；start/end/bonus_calc_date 依學年算出）
    # 學年 N（民國）= 西元 N+1911 年 8 月 ～ N+1912 年 7 月
    # bonus_calc_date 預設為 payout_year 年 1 月 15 日（結算基準日）
    from datetime import date as _date

    civil_start_year = target_academic_year + 1911
    cycle = db.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == target_academic_year)
    )
    if cycle is None:
        cycle = YearEndCycle(
            academic_year=target_academic_year,
            start_date=_date(civil_start_year, 8, 1),
            end_date=_date(civil_start_year + 1, 7, 31),
            bonus_calc_date=_date(payout_year, 1, 15),
        )
        db.add(cycle)
        db.flush()

    earlier_cycle, later_cycle = resolve_target_cycles(db, payout_year)
    earlier_finalized = _cycle_is_finalized(earlier_cycle)
    later_finalized = _cycle_is_finalized(later_cycle)

    written_count = 0
    affected_emp_ids: set[int] = set()
    total = Decimal(0)
    skipped_inactive = 0

    for row in rows:
        if row.is_inactive and row.employee_id not in included_inactive_employee_ids:
            skipped_inactive += 1
            continue

        for bonus_type, amount, summary_id, cycle_finalized, partition in [
            (
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                row.earlier_amount,
                row.earlier_summary_id,
                earlier_finalized,
                "earlier",
            ),
            (
                SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                row.later_amount,
                row.later_summary_id,
                later_finalized,
                "later",
            ),
        ]:
            period_label = map_bonus_type_to_period_label(
                bonus_type, target_academic_year
            )
            source_ref = (
                f"appraisal_summary:{summary_id}"
                if summary_id
                else "appraisal_summary:none"
            )
            # partition 對應的 appraisal cycle（earlier=前一學年上, later=前一學年下）
            appraisal_cycle_id = (
                earlier_cycle.id if partition == "earlier" else later_cycle.id
            )
            calc_meta = {
                "cycle_not_finalized": not cycle_finalized,
                "summary_status": "FINALIZED" if summary_id else "MISSING",
                "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
                "partition": partition,
                "appraisal_cycle_id": appraisal_cycle_id,
            }
            _upsert_special_bonus_item(
                db,
                cycle_id=cycle.id,
                employee_id=row.employee_id,
                bonus_type=bonus_type,
                period_label=period_label,
                amount=amount,
                source_ref=source_ref,
                calc_meta=calc_meta,
                created_by=generated_by,
            )
            written_count += 1

        affected_emp_ids.add(row.employee_id)
        total += row.earlier_amount + row.later_amount

    db.flush()
    return GenerateResult(
        cycle_id=cycle.id,
        generated_count=written_count,
        affected_employee_count=len(affected_emp_ids),
        total_amount=total,
        skipped_inactive_count=skipped_inactive,
        warnings=[],
    )


def void_payouts(db: Session, payout_year: int, voided_by: int) -> int:
    """刪除 target academic_year 下所有 APPRAISAL_HALF_BONUS_* items。

    只刪考核績效獎金兩個 type；不動其他 SpecialBonusType。
    voided_by 由 router 層 audit middleware 處理，此函式接受但不直接使用。
    """
    _advisory_lock_payout(db, payout_year)
    target_academic_year = civil_year_to_target_academic_year(payout_year)
    cycle = db.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == target_academic_year)
    )
    if cycle is None:
        return 0
    items = db.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
    ).all()
    deleted = len(items)
    for item in items:
        db.delete(item)
    db.flush()
    logger.info(
        "void_payouts: deleted %d APPRAISAL_HALF_BONUS items for academic_year=%s (voided_by=%s)",
        deleted,
        target_academic_year,
        voided_by,
    )
    return deleted
