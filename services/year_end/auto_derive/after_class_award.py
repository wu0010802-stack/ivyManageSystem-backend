"""B2 ① 才藝鼓勵獎金（AFTER_CLASS_AWARD）自動推導。

Excel「年終獎金總表」AFTER_CLASS_AWARD：每班 L = J × K，發給該班班導：
  - J = 該班上學期才藝報名人次（COUNT(RegistrationCourse)，**非 distinct**；
        一個學生報兩堂算 2），status IN ('enrolled','promoted_pending')。
  - K = BonusConfig.after_class_award_unit_price（JSON 班名→單價）以 Classroom.name 查。
  - 發放對象 = 該班 ClassEnrollmentTarget(semester_first=True).head_teacher_employee_id。

另：才藝老師段 = 每位列名才藝老師得「全校總人次 × BonusConfig.art_teacher_unit_price」，
收款人由 BonusConfig.art_teacher_employee_ids（employee id list）指定。
list 空/未設或單價未設 → 跳過此段（不報錯）。
split 假設（controller 2026-06-02 決策）：各列名老師各得全額（總人次×單價），非均分。

override 慣例見 auto_derive/__init__.py：source_ref 以 ``auto:`` 標記自動筆；
手動筆（source_ref 非 auto: 開頭）絕不覆寫。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.activity import ActivityRegistration, RegistrationCourse
from models.classroom import Classroom
from models.config import BonusConfig
from models.year_end import (
    ClassEnrollmentTarget,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

# 計入人次的報名課程狀態（waitlist 不計）
_COUNTED_STATUSES = ("enrolled", "promoted_pending")
# 配對成功（合法在班）的報名 match_status 集合（models/activity.py:142 註解）：
#   matched=自動匹配成功 / manual=人工綁定（亦為合法在班）。
#   其餘（pending/rejected/unmatched）視為未配對。J 與 unmatched 以此乾淨互斥。
_SUCCESS_STATUSES = ("matched", "manual")
# 上學期（Excel「N上鼓勵推動才藝班獎金」）
_FIRST_SEMESTER = 1

_SOURCE_REF = "auto:after_class_award"
_Q2 = Decimal("0.01")


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def period_label_for_class(cycle: YearEndCycle, classroom_id: int) -> str:
    """每班一個穩定的 upsert period_label（含 classroom_id 避免多班互蓋）。

    uq 鍵 = (cycle, emp, bonus_type, period_label)；同一班導若帶多班，
    period_label 必須區分班別，否則多班會 upsert 互蓋。
    """
    return f"{cycle.academic_year}上-C{classroom_id}"


# art-teacher 段落用（與每班 award 的 period_label 區隔，避免才藝老師同時是班導時碰撞）
def _art_teacher_period_label(cycle: YearEndCycle) -> str:
    return f"{cycle.academic_year}上-ART"


@dataclass
class AcaReport:
    """① 才藝鼓勵推導結果。

    written          : 寫入/更新的 SpecialBonusItem 筆數（不含 skip 的手動筆）
    unmatched_count  : 未配對（classroom_id IS NULL，或 match_status 不在 {'matched','manual'}）的報名人次
    skipped_manual   : 因手動筆而 skip 的班數
    warnings         : 略過原因（缺單價/缺班導等）
    """

    written: int = 0
    unmatched_count: int = 0
    skipped_manual: int = 0
    warnings: list[str] = field(default_factory=list)


def _latest_active_bonus_config(db: Session) -> Optional[BonusConfig]:
    return db.scalar(
        select(BonusConfig)
        .where(BonusConfig.is_active.is_(True))
        .order_by(BonusConfig.id.desc())
        .limit(1)
    )


def _upsert_auto_item(
    db: Session,
    *,
    cycle_id: int,
    employee_id: int,
    period_label: str,
    amount: Decimal,
    classroom_id: Optional[int],
    calc_meta: dict,
) -> bool:
    """override-aware upsert。

    回傳 True 表示有寫入/更新（新建或更新自動筆）；
    回傳 False 表示既有筆為手動筆而 SKIP（絕不覆寫）。
    """
    existing = db.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle_id,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type == SpecialBonusType.AFTER_CLASS_AWARD,
            SpecialBonusItem.period_label == period_label,
        )
    )
    if existing is None:
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle_id,
                employee_id=employee_id,
                bonus_type=SpecialBonusType.AFTER_CLASS_AWARD,
                period_label=period_label,
                amount=amount,
                classroom_id=classroom_id,
                calc_meta=calc_meta,
                source_ref=_SOURCE_REF,
            )
        )
        return True

    # 既有 row：source_ref 非 auto: 開頭（None 或使用者手填）→ 手動筆，SKIP。
    if not (existing.source_ref or "").startswith("auto:"):
        return False

    existing.amount = amount
    existing.classroom_id = classroom_id
    existing.calc_meta = calc_meta
    existing.source_ref = _SOURCE_REF
    existing.updated_at = now_taipei_naive()
    return True


def _count_enrollments(db: Session, *, classroom_id: int, academic_year: int) -> int:
    """該班上學期報名人次（COUNT(RegistrationCourse)，非 distinct）。

    僅計合法在班（match_status IN ('matched','manual')）的報名，與 unmatched
    乾淨互斥：pending/rejected/unmatched 即使帶 classroom_id 也只進 unmatched，
    不進 J。
    """
    return (
        db.scalar(
            select(func.count(RegistrationCourse.id))
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .where(
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.classroom_id == classroom_id,
                ActivityRegistration.match_status.in_(_SUCCESS_STATUSES),
                ActivityRegistration.school_year == academic_year,
                ActivityRegistration.semester == _FIRST_SEMESTER,
                RegistrationCourse.status.in_(_COUNTED_STATUSES),
            )
        )
        or 0
    )


def _count_unmatched(db: Session, *, academic_year: int) -> int:
    """未配對報名人次（classroom_id IS NULL，或 match_status 不在成功集）。

    與 J 同單位（COUNT(RegistrationCourse)、同狀態/學期），且與 J 乾淨互斥：
    凡不滿足「classroom_id IS NOT NULL AND match_status IN ('matched','manual')」
    者落此（pending/rejected/unmatched 或無班）。語意為「若配對成功則本可計入
    獎金」的報名數。
    """
    return (
        db.scalar(
            select(func.count(RegistrationCourse.id))
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .where(
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.school_year == academic_year,
                ActivityRegistration.semester == _FIRST_SEMESTER,
                RegistrationCourse.status.in_(_COUNTED_STATUSES),
                (
                    (ActivityRegistration.classroom_id.is_(None))
                    | (ActivityRegistration.match_status.not_in(_SUCCESS_STATUSES))
                ),
            )
        )
        or 0
    )


def derive_after_class_award(
    db: Session,
    cycle: YearEndCycle,
    *,
    skip_employee_ids: "set[int] | None" = None,
) -> AcaReport:
    """推導 ① 才藝鼓勵獎金 → upsert special_bonus_items（AFTER_CLASS_AWARD）。

    只 flush（由呼叫端 commit）。idempotent；手動筆不覆寫（見 __init__ override 慣例）。

    skip_employee_ids（P1-2 finalized drift 護欄）：收款人 employee_id 落此集合者
    （該員工的 settlement 非 DRAFT、金額已凍結）→ **不寫/不覆寫**其 auto item，
    避免凍結總額與底層 items 漂移。判定對象為**收款人**：每班 award 看
    head_teacher_employee_id；才藝老師段看各 art emp_id。**只 guard 寫入**，
    total_enrollments 累計與 unmatched_count 全校統計照常計（餵其他收款人/全校報表）。
    """
    skip_ids: set[int] = skip_employee_ids or set()
    report = AcaReport()
    academic_year = cycle.academic_year

    cfg = _latest_active_bonus_config(db)
    unit_prices: dict = {}
    if cfg is not None and cfg.after_class_award_unit_price:
        unit_prices = dict(cfg.after_class_award_unit_price)
    else:
        report.warnings.append("無 BonusConfig.after_class_award_unit_price 單價設定")

    # 每班：以 ClassEnrollmentTarget(semester_first=True) 定義班集合 + 取班導
    targets = list(
        db.scalars(
            select(ClassEnrollmentTarget).where(
                ClassEnrollmentTarget.year_end_cycle_id == cycle.id,
                ClassEnrollmentTarget.semester_first.is_(True),
            )
        )
    )

    total_enrollments = 0
    for tgt in targets:
        classroom = db.get(Classroom, tgt.classroom_id)
        if classroom is None:
            report.warnings.append(f"classroom_id={tgt.classroom_id} 不存在，略過")
            continue

        j = _count_enrollments(
            db, classroom_id=tgt.classroom_id, academic_year=academic_year
        )
        total_enrollments += j  # 全校總人次（餵才藝老師段）— 不受 skip 影響

        if tgt.head_teacher_employee_id is None:
            report.warnings.append(f"班「{classroom.name}」無班導，略過")
            continue

        # P1-2：收款人（班導）settlement 非 DRAFT（凍結）→ 不寫其 auto item。
        # 須在 total_enrollments 累計之後（上方）才 skip，避免影響才藝老師段/全校報表。
        if tgt.head_teacher_employee_id in skip_ids:
            continue

        if classroom.name not in unit_prices:
            report.warnings.append(f"班「{classroom.name}」無單價設定，略過")
            continue

        k = unit_prices[classroom.name]
        amount = _q2(Decimal(str(j)) * Decimal(str(k)))
        wrote = _upsert_auto_item(
            db,
            cycle_id=cycle.id,
            employee_id=tgt.head_teacher_employee_id,
            period_label=period_label_for_class(cycle, tgt.classroom_id),
            amount=amount,
            classroom_id=tgt.classroom_id,
            calc_meta={"J": j, "K": k},
        )
        if wrote:
            report.written += 1
        else:
            report.skipped_manual += 1

    # 未配對報名人次
    report.unmatched_count = _count_unmatched(db, academic_year=academic_year)

    # 才藝老師單價段：每位列名才藝老師得「全校總人次 × art_teacher_unit_price」。
    # 收款人由 BonusConfig.art_teacher_employee_ids（employee id list）指定；
    # list 空/未設或單價未設 → 跳過（不報錯，維持既有安全行為）。
    # split 假設：各列名老師各得全額（總人次×單價），非均分（controller 2026-06-02 決策）。
    art_price = getattr(cfg, "art_teacher_unit_price", None) if cfg else None
    art_ids = getattr(cfg, "art_teacher_employee_ids", None) if cfg else None
    if art_price and art_ids:
        art_amount = _q2(Decimal(str(total_enrollments)) * Decimal(str(art_price)))
        art_label = _art_teacher_period_label(cycle)
        for emp_id in art_ids:
            # P1-2：才藝老師（收款人）settlement 非 DRAFT（凍結）→ 不寫其 auto item。
            if emp_id in skip_ids:
                continue
            wrote = _upsert_auto_item(
                db,
                cycle_id=cycle.id,
                employee_id=emp_id,
                period_label=art_label,
                amount=art_amount,
                classroom_id=None,
                calc_meta={"total_J": total_enrollments, "art_unit_price": art_price},
            )
            if wrote:
                report.written += 1
            else:
                report.skipped_manual += 1
    elif art_price and not art_ids:
        report.warnings.append(
            "已設 art_teacher_unit_price 但未指定 art_teacher_employee_ids → 跳過才藝老師段"
        )

    db.flush()
    logger.info(
        "after_class_award derive: cycle=%s written=%d unmatched=%d skipped_manual=%d total_enroll=%d",
        cycle.academic_year,
        report.written,
        report.unmatched_count,
        report.skipped_manual,
        total_enrollments,
    )
    return report
