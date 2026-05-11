"""教職員考核 service：純 function（無 IO 的部分）+ 需要 session 的 helpers。

- 評分切點 (classify_grade)
- 獎金計算 (compute_bonus_amount, load_active_rates_map)
- 學期預設日期 (default_cycle_dates / default_calc_date)
- 職稱 → role_group 推薦 (suggest_role_group)
- summary 重算與 stale 標記 (recompute_summary / mark_summary_stale)
- 解聘 trigger 檢查 (check_termination_threshold)

純 function 無狀態。資料庫操作由 caller 注入 session。
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalBonusRate,
    AppraisalEvent,
    AppraisalParticipant,
    AppraisalSummary,
    EventType,
    Grade,
    RoleGroup,
    Semester,
    SummaryStatus,
)

logger = logging.getLogger(__name__)


# ===== 評分切點 =====


def classify_grade(total_score: Decimal) -> Grade:
    """依總分回傳等級。文件規則：優≥90 / 甲80-89 / 乙70-79 / 丙60-69 / 丁<60。"""
    if total_score >= Decimal("90"):
        return Grade.OUTSTANDING
    if total_score >= Decimal("80"):
        return Grade.GOOD
    if total_score >= Decimal("70"):
        return Grade.PASS
    if total_score >= Decimal("60"):
        return Grade.WARN
    return Grade.FAIL


# ===== 獎金 =====


def compute_bonus_amount(
    total_score: Decimal,
    grade: Grade,
    role_group: RoleGroup,
    rates_map: dict[tuple[RoleGroup, Grade], Decimal],
) -> Decimal:
    """獎金 = base_amount × (total_score / 100)。乙等以下回 0。

    rates_map 沒對應 key 時也回 0（避免 KeyError，讓設定缺漏不擋計算）。
    """
    if grade not in (Grade.OUTSTANDING, Grade.GOOD):
        return Decimal("0")
    base = rates_map.get((role_group, grade))
    if base is None:
        return Decimal("0")
    return (base * total_score / Decimal("100")).quantize(Decimal("0.01"))


def load_active_rates_map(
    db: Session, on_date: Optional[date] = None
) -> dict[tuple[RoleGroup, Grade], Decimal]:
    """取 effective_from <= on_date 中最新一版 rate，組成 lookup map。"""
    on_date = on_date or date.today()
    rows = (
        db.execute(
            select(AppraisalBonusRate)
            .where(AppraisalBonusRate.effective_from <= on_date)
            .order_by(AppraisalBonusRate.effective_from.desc())
        )
        .scalars()
        .all()
    )
    seen: dict[tuple[RoleGroup, Grade], Decimal] = {}
    for r in rows:
        key = (r.role_group, r.grade)
        # 最新版優先（已依 effective_from desc 排序，setdefault 取第一個即最新）
        seen.setdefault(key, r.base_amount)
    return seen


# ===== 學期預設日期 =====


def default_cycle_dates(
    academic_year: int,
    semester,
) -> tuple[date, date, date]:
    """第一學期 8/1 ~ 翌年 1/31；第二學期 2/1 ~ 7/31。

    academic_year 為民國年（114 = 西元 2025）。
    base_score_calc_date 預設 9/15（上）/ 3/15（下）。
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
    """單獨計算核計日（給設定 UI 用）。"""
    sem_value = semester.value if isinstance(semester, Semester) else semester
    if sem_value == "FIRST":
        return date(year_gregorian, 9, 15)
    return date(year_gregorian, 3, 15)


# ===== 職稱 → role_group =====

_SUPERVISOR_KEYWORDS = ("園長", "主任", "執行長", "總園長")
_HEAD_TEACHER_KEYWORDS = ("班導師", "行政會計", "會計", "班導")


def _kw_in_title_not_preceded_by_fu(title: str, kw: str) -> bool:
    """判斷 kw 是否出現在 title 中，且出現位置前一個字不是「副」。

    用來區分「班導師」（HEAD_TEACHER）與「副班導師」（ASSISTANT）。
    """
    idx = title.find(kw)
    while idx != -1:
        if idx == 0 or title[idx - 1] != "副":
            return True
        idx = title.find(kw, idx + 1)
    return False


def suggest_role_group(title: Optional[str]) -> RoleGroup:
    """依職稱字串推薦考核 role_group。未知 / None / 空字串 → ASSISTANT（保守預設）。

    含「副」前綴的班導師（副班導師）歸 ASSISTANT 而非 HEAD_TEACHER。
    """
    if not title:
        return RoleGroup.ASSISTANT
    for kw in _SUPERVISOR_KEYWORDS:
        if kw in title:
            return RoleGroup.SUPERVISOR
    for kw in _HEAD_TEACHER_KEYWORDS:
        if _kw_in_title_not_preceded_by_fu(title, kw):
            return RoleGroup.HEAD_TEACHER
    return RoleGroup.ASSISTANT


# ===== Summary 重算 =====


def recompute_summary(
    db: Session,
    summary: AppraisalSummary,
    participant: AppraisalParticipant,
    rates_map: Optional[dict[tuple[RoleGroup, Grade], Decimal]] = None,
) -> AppraisalSummary:
    """重算 summary：base + 所有未撤銷 event score_delta = total → grade → bonus。

    呼叫端負責 commit。已 FINALIZED 不可呼叫此 function（caller 自檢）。
    """
    if summary.status == SummaryStatus.FINALIZED:
        raise ValueError("Cannot recompute FINALIZED summary")
    rates_map = rates_map if rates_map is not None else load_active_rates_map(db)

    event_deltas = (
        db.execute(
            select(AppraisalEvent.score_delta).where(
                AppraisalEvent.participant_id == participant.id,
                AppraisalEvent.reverted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    total_delta = sum(event_deltas, start=Decimal("0"))

    summary.base_score = participant.base_score
    summary.event_score_sum = Decimal(total_delta).quantize(Decimal("0.01"))
    summary.total_score = (summary.base_score + summary.event_score_sum).quantize(
        Decimal("0.01")
    )
    summary.grade = classify_grade(summary.total_score)
    summary.bonus_amount = compute_bonus_amount(
        summary.total_score, summary.grade, participant.role_group, rates_map
    )
    summary.version += 1
    return summary


def mark_summary_stale(
    db: Session,
    participant_id: int,
    *,
    reset_signatures: bool = True,
) -> Optional[AppraisalSummary]:
    """事件變更時呼叫：summary 重算 + status reset 到 DRAFT。

    若 FINALIZED → raise PermissionError；caller 應在事件 patch 時擋下並回 409。
    """
    summary = db.execute(
        select(AppraisalSummary).where(
            AppraisalSummary.participant_id == participant_id
        )
    ).scalar_one_or_none()
    if summary is None:
        return None
    if summary.status == SummaryStatus.FINALIZED:
        raise PermissionError("summary_finalized:cannot_modify_underlying_events")
    participant = db.get(AppraisalParticipant, participant_id)
    recompute_summary(db, summary, participant)
    if reset_signatures and summary.status != SummaryStatus.DRAFT:
        summary.status = SummaryStatus.DRAFT
        summary.supervisor_signed_at = None
        summary.supervisor_signed_by = None
        summary.supervisor_comment = None
        summary.accounting_signed_at = None
        summary.accounting_signed_by = None
        summary.accounting_comment = None
    return summary


# ===== 解聘 trigger =====


def check_termination_threshold(
    db: Session,
    participant: AppraisalParticipant,
) -> bool:
    """檢查 cycle 內累計 MAJOR_DEMERIT 是否 ≥ 2，回 True 表示觸發通知。

    僅返回是否觸發；通知由 caller 發（避免循環依賴 notification service）。
    """
    rows = db.execute(
        select(AppraisalEvent.id).where(
            AppraisalEvent.participant_id == participant.id,
            AppraisalEvent.event_type == EventType.MAJOR_DEMERIT,
            AppraisalEvent.reverted_at.is_(None),
        )
    ).all()
    return len(rows) >= 2
