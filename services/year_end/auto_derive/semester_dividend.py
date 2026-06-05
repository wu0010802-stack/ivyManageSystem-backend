"""B4 ④ 學期紅利 semester_dividend 自動推導。

Excel「113上.113下學期紅利獎金」逐學期、每班導：
  - 舊生率 ≥ 門檻（BonusConfig.dividend_returning_threshold，預設 0.9）
        → + dividend_returning_amount（預設 500）
  - 才藝班參加率 ≥ 門檻（dividend_activity_threshold，預設 0.8）
        → + dividend_activity_amount（預設 1000）
  - 小計 = 兩者加總；蔡宜倩上學期 = 500 + 1000 = 1500。
  - office/非帶班 = 0：無 ClassEnrollmentTarget → 不進迴圈（不寫筆）。

---------------------------------------------------------------------------
**舊生率（returning rate）**
---------------------------------------------------------------------------
直接讀 ``ClassEnrollmentTarget.returning_student_rate``（B6 已寫的小數，如 0.917）。
本模組不重算舊生率（單一責任：B6 算、B4 消費）。

---------------------------------------------------------------------------
**才藝率（activity rate）= distinct 學生參加率（非人次！）**
---------------------------------------------------------------------------
復用 ``services/appraisal/status_aggregator._aggregate_activity_rate`` 的 query
語意（等價重寫為 fraction）：
  分子 = COUNT(DISTINCT ActivityRegistration.student_id)
         WHERE classroom_id == 該班 AND school_year == cycle.academic_year
               AND semester == (1 if semester_first else 2)
               AND is_active AND student_id IS NOT NULL
  分母 = 該班 active 學生數
         WHERE Student.classroom_id == 該班 AND lifecycle_status == active
回 **fraction**（0.xxx，與門檻/returning_student_rate 同單位），**不是** 百分比。

⚠️ 與 B2（① 才藝鼓勵）刻意不同：B2 是 **人次**（COUNT RegistrationCourse，一生報
兩堂算 2）；B4 是 **distinct 學生參加率**（一生報幾堂都算 1）。語意不同。

分母採 status_aggregator 的「現態 active」（lifecycle_status==active），非 B6 的
基準日 point-in-time filter——對齊既有考核才藝率算法（self-eval：因 status_aggregator
無 per-semester 分母，FIRST/SECOND 兩列共用同一分母；可接受）。

---------------------------------------------------------------------------
**門檻比較**
---------------------------------------------------------------------------
``≥`` 門檻（含等於）。一律 ``Decimal(str(cfg.field))`` 轉 Decimal 避免 float 邊界；
0.900 == 門檻 0.9 → 達標；0.899 < 0.9 → 不達標。

---------------------------------------------------------------------------
**override 慣例（與 B2/B3/B6 一致）**
---------------------------------------------------------------------------
source_ref 前綴 ``"auto:semester_dividend"``。upsert（uq 鍵
(cycle, employee, bonus_type, period_label)）：
  - 既有 row source_ref 非 ``"auto:"`` 開頭（None 或手填）→ 手動筆，SKIP（絕不覆寫）。
  - source_ref ``"auto:"`` 開頭 → UPDATE。
  - 不存在 → 新建。
紅利 = 0 仍寫一筆 0 元（stale-safe，比照 B2 always-write；re-run 會更新、grid 可過濾）。
bonus_type = SEMESTER_DIVIDEND_FIRST（上學期）/ SEMESTER_DIVIDEND_SECOND（下學期）。
period_label 含 classroom_id（同班導同學期多班不互蓋）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.activity import ActivityRegistration
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.config import BonusConfig
from models.year_end import (
    ClassEnrollmentTarget,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

_SOURCE_REF = "auto:semester_dividend"
_Q2 = Decimal("0.01")
_Q3 = Decimal("0.001")  # 才藝率 fraction 顯示精度（calc_meta / 比較皆用原始除法）

# config 預設值（B1 column default；config 缺欄位時 fallback 用）
_DEFAULT_RETURNING_THRESHOLD = Decimal("0.9")
_DEFAULT_RETURNING_AMOUNT = Decimal("500")
_DEFAULT_ACTIVITY_THRESHOLD = Decimal("0.8")
_DEFAULT_ACTIVITY_AMOUNT = Decimal("1000")


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _q3(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q3, rounding=ROUND_HALF_UP)


def _dec(x, default: Decimal) -> Decimal:
    """config 欄位（float/None）→ Decimal；None 用 default。經 str() 避免 float 噪音。"""
    if x is None:
        return default
    return Decimal(str(x))


def period_label_for_class(
    cycle: YearEndCycle, classroom_id: int, *, semester_first: bool
) -> str:
    """每班每學期一個穩定的 upsert period_label。

    uq 鍵 = (cycle, emp, bonus_type, period_label)；bonus_type 已分 FIRST/SECOND，
    但同班導同學期帶多班時 period_label 仍須含 classroom_id 避免多班互蓋。
    """
    sem = "上" if semester_first else "下"
    return f"{cycle.academic_year}{sem}-C{classroom_id}"


@dataclass
class SemesterDividendReport:
    """④ 學期紅利推導結果。

    written        : 寫入/更新的 SpecialBonusItem 筆數（不含 skip 的手動筆）
    skipped_manual : 因手動筆而 skip 的筆數
    warnings       : 略過原因（無班導/班不存在等）
    """

    written: int = 0
    skipped_manual: int = 0
    warnings: list[str] = field(default_factory=list)


def _latest_active_bonus_config(db: Session) -> Optional[BonusConfig]:
    return db.scalar(
        select(BonusConfig)
        .where(BonusConfig.is_active.is_(True))
        .order_by(BonusConfig.id.desc())
        .limit(1)
    )


def _activity_rate(
    db: Session, *, classroom_id: int, academic_year: int, semester: int
) -> tuple[Decimal, int, int]:
    """該班 distinct 才藝參加率（fraction）+ (registered_distinct, enrolled)。

    分子 = COUNT(DISTINCT ActivityRegistration.student_id)（該班/學年/學期/is_active/
           student_id 非 NULL）—— distinct 學生（**非人次**）。
    分母 = 該班 active 學生數（lifecycle_status==active）。
    對齊 status_aggregator._aggregate_activity_rate 的 query 語意，但回 fraction。
    分母 0 → 回 (Decimal('0'), 0, 0)（除零保護；無學生視為 0 才藝率）。
    """
    enrolled = int(
        db.scalar(
            select(func.count(Student.id)).where(
                Student.classroom_id == classroom_id,
                Student.lifecycle_status == LIFECYCLE_ACTIVE,
            )
        )
        or 0
    )
    registered = int(
        db.scalar(
            select(func.count(func.distinct(ActivityRegistration.student_id))).where(
                ActivityRegistration.classroom_id == classroom_id,
                ActivityRegistration.school_year == academic_year,
                ActivityRegistration.semester == semester,
                ActivityRegistration.student_id.is_not(None),
                ActivityRegistration.is_active.is_(True),
            )
        )
        or 0
    )
    if enrolled <= 0:
        return Decimal("0"), registered, enrolled
    rate = Decimal(registered) / Decimal(enrolled)
    return rate, registered, enrolled


def _upsert_auto_item(
    db: Session,
    *,
    cycle_id: int,
    employee_id: int,
    bonus_type: SpecialBonusType,
    period_label: str,
    amount: Decimal,
    classroom_id: Optional[int],
    calc_meta: dict,
) -> bool:
    """override-aware upsert（bonus_type 參數化以支援 FIRST/SECOND）。

    回傳 True 表示有寫入/更新（新建或更新自動筆）；
    回傳 False 表示既有筆為手動筆而 SKIP（絕不覆寫）。
    """
    existing = db.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle_id,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type == bonus_type,
            SpecialBonusItem.period_label == period_label,
        )
    )
    if existing is None:
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle_id,
                employee_id=employee_id,
                bonus_type=bonus_type,
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


def derive_semester_dividend(
    db: Session,
    cycle: YearEndCycle,
    *,
    skip_employee_ids: "set[int] | None" = None,
) -> SemesterDividendReport:
    """推導 ④ 學期紅利 → upsert special_bonus_items（SEMESTER_DIVIDEND_FIRST/SECOND）。

    只 flush（由呼叫端 commit）。idempotent；手動筆不覆寫。逐 ClassEnrollmentTarget
    （FIRST/SECOND 各列各算），舊生率讀 returning_student_rate、才藝率現算 distinct。

    skip_employee_ids（P1-2 finalized drift 護欄）：收款人（班導
    head_teacher_employee_id）settlement 非 DRAFT（金額已凍結）→ **不寫/不覆寫**其
    auto item，避免凍結總額與底層 items 漂移。
    """
    skip_ids: set[int] = skip_employee_ids or set()
    report = SemesterDividendReport()
    academic_year = cycle.academic_year

    from services.year_end.settlement_builder import bonus_config_for_academic_year

    cfg = bonus_config_for_academic_year(db, academic_year)
    if cfg is None:
        report.warnings.append("無 active BonusConfig，使用內建門檻/金額預設")
    ret_threshold = _dec(
        getattr(cfg, "dividend_returning_threshold", None), _DEFAULT_RETURNING_THRESHOLD
    )
    ret_amount = _dec(
        getattr(cfg, "dividend_returning_amount", None), _DEFAULT_RETURNING_AMOUNT
    )
    act_threshold = _dec(
        getattr(cfg, "dividend_activity_threshold", None), _DEFAULT_ACTIVITY_THRESHOLD
    )
    act_amount = _dec(
        getattr(cfg, "dividend_activity_amount", None), _DEFAULT_ACTIVITY_AMOUNT
    )

    targets = list(
        db.scalars(
            select(ClassEnrollmentTarget).where(
                ClassEnrollmentTarget.year_end_cycle_id == cycle.id
            )
        )
    )

    for tgt in targets:
        if tgt.head_teacher_employee_id is None:
            report.warnings.append(
                f"班 classroom_id={tgt.classroom_id} "
                f"(semester_first={tgt.semester_first}) 無班導，略過"
            )
            continue

        # P1-2：收款人（班導）settlement 非 DRAFT（凍結）→ 不寫其 auto item。
        if tgt.head_teacher_employee_id in skip_ids:
            continue

        classroom = db.get(Classroom, tgt.classroom_id)
        if classroom is None:
            report.warnings.append(f"classroom_id={tgt.classroom_id} 不存在，略過")
            continue

        semester = 1 if tgt.semester_first else 2
        bonus_type = (
            SpecialBonusType.SEMESTER_DIVIDEND_FIRST
            if tgt.semester_first
            else SpecialBonusType.SEMESTER_DIVIDEND_SECOND
        )

        # 舊生率：直接讀 B6 寫入的小數
        returning_rate = Decimal(str(tgt.returning_student_rate or 0))
        # 才藝率：現算 distinct 學生參加率（fraction）
        activity_rate, registered, enrolled = _activity_rate(
            db,
            classroom_id=tgt.classroom_id,
            academic_year=academic_year,
            semester=semester,
        )

        # ≥ 門檻（含等於）；以原始 Decimal 比較，避免量化翻轉邊界
        returning_qualified = returning_rate >= ret_threshold
        activity_qualified = activity_rate >= act_threshold
        amount = _q2(
            (ret_amount if returning_qualified else Decimal("0"))
            + (act_amount if activity_qualified else Decimal("0"))
        )

        calc_meta = {
            "returning_rate": str(_q3(returning_rate)),
            "activity_rate": str(_q3(activity_rate)),
            "returning_threshold": str(ret_threshold),
            "activity_threshold": str(act_threshold),
            "returning_qualified": returning_qualified,
            "activity_qualified": activity_qualified,
            "registered_distinct": registered,
            "enrolled_students": enrolled,
            "semester": semester,
        }

        wrote = _upsert_auto_item(
            db,
            cycle_id=cycle.id,
            employee_id=tgt.head_teacher_employee_id,
            bonus_type=bonus_type,
            period_label=period_label_for_class(
                cycle, tgt.classroom_id, semester_first=tgt.semester_first
            ),
            amount=amount,
            classroom_id=tgt.classroom_id,
            calc_meta=calc_meta,
        )
        if wrote:
            report.written += 1
        else:
            report.skipped_manual += 1

    db.flush()
    logger.info(
        "semester_dividend derive: cycle=%s written=%d skipped_manual=%d",
        cycle.academic_year,
        report.written,
        report.skipped_manual,
    )
    return report
