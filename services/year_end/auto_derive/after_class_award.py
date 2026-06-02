"""B2 ① 才藝鼓勵獎金（AFTER_CLASS_AWARD）自動推導。

Excel「年終獎金總表」AFTER_CLASS_AWARD：每班 L = J × K，發給該班班導：
  - J = 該班上學期才藝報名人次（COUNT(RegistrationCourse)，**非 distinct**；
        一個學生報兩堂算 2），status IN ('enrolled','promoted_pending')。
  - K = BonusConfig.after_class_award_unit_price（JSON 班名→單價）以 Classroom.name 查。
  - 發放對象 = 該班 ClassEnrollmentTarget(semester_first=True).head_teacher_employee_id。

另：才藝老師單價 = 全校總人次 × BonusConfig.art_teacher_unit_price，
發給「設定指定的才藝老師」。目前設定無 art-teacher 收款人欄位 → 永遠跳過此段
（task 允許「若設定未指定才藝老師則跳過此段（不報錯）」），待 controller 決定
收款人如何指定後補上。

override 慣例見 auto_derive/__init__.py：source_ref 以 ``auto:`` 標記自動筆；
手動筆（source_ref 非 auto: 開頭）絕不覆寫。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)

# 計入人次的報名課程狀態（waitlist 不計）
_COUNTED_STATUSES = ("enrolled", "promoted_pending")
# 配對成功的報名 match_status 字面（models/activity.py:143）
_MATCHED_STATUS = "matched"
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
    unmatched_count  : 未配對（classroom_id IS NULL 或 match_status != matched）的報名人次
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
    existing.updated_at = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return True


def _count_enrollments(db: Session, *, classroom_id: int, academic_year: int) -> int:
    """該班上學期報名人次（COUNT(RegistrationCourse)，非 distinct）。"""
    return (
        db.scalar(
            select(func.count(RegistrationCourse.id))
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .where(
                ActivityRegistration.classroom_id == classroom_id,
                ActivityRegistration.school_year == academic_year,
                ActivityRegistration.semester == _FIRST_SEMESTER,
                RegistrationCourse.status.in_(_COUNTED_STATUSES),
            )
        )
        or 0
    )


def _count_unmatched(db: Session, *, academic_year: int) -> int:
    """未配對報名人次（classroom_id IS NULL 或 match_status != matched）。

    與 J 同單位（COUNT(RegistrationCourse)、同狀態/學期），語意為
    「若配對成功則本可計入獎金」的報名數。
    """
    return (
        db.scalar(
            select(func.count(RegistrationCourse.id))
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .where(
                ActivityRegistration.school_year == academic_year,
                ActivityRegistration.semester == _FIRST_SEMESTER,
                RegistrationCourse.status.in_(_COUNTED_STATUSES),
                (
                    (ActivityRegistration.classroom_id.is_(None))
                    | (ActivityRegistration.match_status != _MATCHED_STATUS)
                ),
            )
        )
        or 0
    )


def derive_after_class_award(db: Session, cycle: YearEndCycle) -> AcaReport:
    """推導 ① 才藝鼓勵獎金 → upsert special_bonus_items（AFTER_CLASS_AWARD）。

    只 flush（由呼叫端 commit）。idempotent；手動筆不覆寫（見 __init__ override 慣例）。
    """
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
        total_enrollments += j

        if tgt.head_teacher_employee_id is None:
            report.warnings.append(f"班「{classroom.name}」無班導，略過")
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

    # 才藝老師單價段：全校總人次 × art_teacher_unit_price，發給設定指定的才藝老師。
    # 目前設定無 art-teacher 收款人欄位 → 永遠跳過（task 允許）。待 controller 決定。
    art_price = getattr(cfg, "art_teacher_unit_price", None) if cfg else None
    if art_price:
        report.warnings.append(
            "已設 art_teacher_unit_price，但設定無 art-teacher 收款人欄位 → 跳過才藝老師段"
        )
    # （無收款人解析來源；不報錯，不寫 item）

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
