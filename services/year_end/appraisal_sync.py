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
from models.year_end import (
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)

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


def _frozen_settlement_employee_ids(db: Session, year_end_cycle_id: int) -> set[int]:
    """回傳該 year_end cycle 下 settlement 已凍結（非 DRAFT）的 employee_id 集合。

    P2（2026-06-16）：generate/void payout 不得改動已簽核（SUPERVISOR/ACCOUNTING_SIGNED）
    或已核定（FINALIZED）年終的 APPRAISAL_HALF 明細——此時 settlement.total_amount 已凍結
    （build_settlements 對非 DRAFT settlement skip），若仍覆寫/硬刪明細，匯出總表
    （讀 settlement.total_amount）與明細條（讀 special_bonus_items）會對不起來。
    """
    rows = db.scalars(
        select(YearEndSettlement.employee_id).where(
            YearEndSettlement.year_end_cycle_id == year_end_cycle_id,
            YearEndSettlement.status != YearEndSettlementStatus.DRAFT,
        )
    ).all()
    return set(rows)


def _recompute_draft_settlement_total(
    db: Session,
    year_end_cycle_id: int,
    employee_id: int,
    total_by_emp: dict[int, Decimal] | None = None,
) -> None:
    """generate/void 改動 SpecialBonusItem 後，同步重算對應 DRAFT settlement 的
    special_bonus_total / total_amount（#1，2026-06-16）。

    口徑：special_bonus_total = compute_special_bonus_total_by_emp（excel-wins 去重，
    有 Excel 列即排除同型 auto 列）；total_amount = payable_amount + special_bonus_total。
    與 build_settlements / api `_recompute_settlement_special_total` 同口徑（qa-loop #1，
    2026-06-23：舊版裸 SUM 會把 Excel+auto 同型雙計 → total_amount 多發）。

    只動 DRAFT settlement：
    - settlement 不存在 → no-op（建立 settlement 時會主動回算既有 special bonus）。
    - 非 DRAFT（已簽核/核定）→ 不動。凍結員工的明細在 generate/void 已被跳過
      （見 _frozen_settlement_employee_ids），total_amount 沿用定案值，不得重算
      （否則簽章還在卻改了轉帳金額）。
    """
    settlement = db.scalar(
        select(YearEndSettlement).where(
            YearEndSettlement.year_end_cycle_id == year_end_cycle_id,
            YearEndSettlement.employee_id == employee_id,
        )
    )
    if settlement is None:
        return
    if settlement.status != YearEndSettlementStatus.DRAFT:
        return
    # qa-loop #1（2026-06-23）：與 canonical build_settlements 同口徑套 excel-wins 去重，
    # 避免 Excel 列 + 同型 auto 列雙計 → settlement.total_amount 灌大 → 轉帳名冊多發。
    # A2（2026-06-29 效能健檢）：批次 caller 預先算好整 cycle 的 emp→total dict 傳入，
    # 避免每位員工各掃一次全 cycle SpecialBonusItem（O(員工×全表)）。未傳則自掃一次
    # （單筆 caller，如 api/year_end 與既有測試，維持原行為）。
    if total_by_emp is None:
        from services.year_end.settlement_builder import (
            compute_special_bonus_total_by_emp,
        )

        total_by_emp = compute_special_bonus_total_by_emp(db, year_end_cycle_id)

    total_sum = total_by_emp.get(int(employee_id), Decimal("0"))
    settlement.special_bonus_total = total_sum
    settlement.total_amount = settlement.payable_amount + total_sum


def _recompute_draft_settlement_totals_bulk(
    db: Session, year_end_cycle_id: int, employee_ids
) -> None:
    """對多名員工一次重算 DRAFT settlement total（A2，2026-06-29 效能健檢）。

    `compute_special_bonus_total_by_emp` 對整個 cycle 全員只掃**一次**，取代
    per-employee 各掃一次的 O(受影響員工 × 全 SpecialBonusItem 全表)。口徑與單筆
    `_recompute_draft_settlement_total` 一致（excel-wins 去重，由 test_year_end_recompute_excel_wins
    固化）。空名單即 no-op，不發無謂的全表掃描。
    """
    employee_ids = list(employee_ids)
    if not employee_ids:
        return
    from services.year_end.settlement_builder import (
        compute_special_bonus_total_by_emp,
    )

    total_by_emp = compute_special_bonus_total_by_emp(db, year_end_cycle_id)
    for emp_id in employee_ids:
        _recompute_draft_settlement_total(
            db, year_end_cycle_id, emp_id, total_by_emp=total_by_emp
        )


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


class PayoutNotFinalizedError(Exception):
    """考核 summary 未 FINALIZED 即嘗試產生年終 payout（防跳過三簽發放考核年終）。"""


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

    # P2-4（2026-06-15 運作探測）硬閘：任何存在的 summary 未 FINALIZED 即拒絕產生
    # payout——防跳過三簽（會計簽核）發放考核年終獎金。在任何寫入前觸發，確保零落地。
    not_finalized = [
        r.employee_id
        for r in rows
        if "earlier_summary_not_finalized" in r.warnings
        or "later_summary_not_finalized" in r.warnings
    ]
    if not_finalized:
        raise PayoutNotFinalizedError(
            f"{len(not_finalized)} 名員工的考核 summary 尚未 FINALIZED，"
            "不可產生年終考核獎金 payout；請先完成三簽"
        )

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

    # P2：已簽核/核定年終的員工，其 APPRAISAL_HALF 明細已凍結，不得覆寫。
    frozen_emp_ids = _frozen_settlement_employee_ids(db, cycle.id)

    written_count = 0
    affected_emp_ids: set[int] = set()
    total = Decimal(0)
    skipped_inactive = 0
    skipped_frozen = 0
    warnings_acc: list[str] = []

    for row in rows:
        if row.is_inactive and row.employee_id not in included_inactive_employee_ids:
            skipped_inactive += 1
            continue

        if row.employee_id in frozen_emp_ids:
            # 對應年終已簽核/核定 → 不覆寫明細，避免總額/明細漂移。
            skipped_frozen += 1
            warnings_acc.append(f"frozen_settlement_skipped:{row.employee_id}")
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
            # summary_status 須反映 summary 真實狀態，不可只看「有無 id」就標
            # FINALIZED。硬閘（PayoutNotFinalizedError，本檔 generate_payouts 開頭）已對
            # DRAFT/未 FINALIZED summary 擋下整批；此處誠實推導 + 對 MISSING 歸零為縱深防禦。
            if summary_id is None:
                summary_status = "MISSING"
            elif f"{partition}_summary_not_finalized" in row.warnings:
                summary_status = "NOT_FINALIZED"
            else:
                summary_status = "FINALIZED"
            # #9 + #10（2026-06-16）fail-safe：只有 FINALIZED 的考核 summary 金額才得
            # 流入可轉帳金額。MISSING / NOT_FINALIZED（DRAFT 等未定案）一律視為 0，
            # 避免未定案績效被計入年終轉帳。與 year_end excel_io 既有 fail-safe 風格一致
            # （不硬性 422 阻擋整個端點）；缺漏/未定案僅記 warning + 寫 0。
            payout_amount = amount if summary_status == "FINALIZED" else Decimal(0)
            calc_meta = {
                "cycle_not_finalized": not cycle_finalized,
                "summary_status": summary_status,
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
                amount=payout_amount,
                source_ref=source_ref,
                calc_meta=calc_meta,
                created_by=generated_by,
            )
            written_count += 1
            total += payout_amount

        # P3：把 preview 偵測到的 warning（summary 未核定、單 cycle 參與等）帶出，
        # 不再回傳寫死的空 list。
        warnings_acc.extend(row.warnings)
        affected_emp_ids.add(row.employee_id)

    # 決策⑥B（2026-06-02）後考核獎金走 year_end settlement 表外發放，
    # 不進月薪資、不進二代健保補充保費累計（CLAUDE.md §10/§11），
    # 故不再標記薪資 needs_recalc。

    if skipped_frozen:
        logger.info(
            "generate_payouts: skipped %d employees with frozen (non-DRAFT) "
            "year_end settlement (cycle_id=%s)",
            skipped_frozen,
            cycle.id,
        )

    # #1（2026-06-16）：對受影響（非凍結）員工同步重算 DRAFT settlement 的
    # special_bonus_total / total_amount，避免轉帳名冊（讀 settlement.total_amount）
    # 與明細條（即時 aggregate SpecialBonusItem）漂移。凍結員工已被跳過、不在
    # affected_emp_ids，且 _recompute_draft_settlement_total 對非 DRAFT no-op。
    _recompute_draft_settlement_totals_bulk(db, cycle.id, affected_emp_ids)

    db.flush()
    return GenerateResult(
        cycle_id=cycle.id,
        generated_count=written_count,
        affected_employee_count=len(affected_emp_ids),
        total_amount=total,
        skipped_inactive_count=skipped_inactive,
        warnings=sorted(set(warnings_acc)),
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
    # P2：已簽核/核定年終的員工，其明細已凍結，不得硬刪（否則 settlement.total_amount
    # 仍為舊值，總表與明細漂移）。只刪非凍結員工的明細。
    frozen_emp_ids = _frozen_settlement_employee_ids(db, cycle.id)
    deletable = [item for item in items if item.employee_id not in frozen_emp_ids]
    skipped_frozen = len(items) - len(deletable)
    deleted = len(deletable)
    affected_emp_ids = {item.employee_id for item in deletable}
    for item in deletable:
        db.delete(item)
    db.flush()
    # #1（2026-06-16）：刪除後同步重算受影響（非凍結）員工的 DRAFT settlement total，
    # 讓 settlement.total_amount 回到不含已刪 APPRAISAL_HALF 的金額（避免名冊殘留舊值）。
    _recompute_draft_settlement_totals_bulk(db, cycle.id, affected_emp_ids)
    db.flush()
    logger.info(
        "void_payouts: deleted %d APPRAISAL_HALF_BONUS items for academic_year=%s "
        "(voided_by=%s, skipped_frozen=%d)",
        deleted,
        target_academic_year,
        voided_by,
        skipped_frozen,
    )
    return deleted
