"""B6 ⑥ 班級舊生率（returning_student_rate）自動推導 + 全校舊生率 helper。

Excel「班級經營績效 114.01.15」舊生（預繳）註冊率：天堂鳥 0.917 / 茉莉 1 /
牡丹 1 / 薔薇 0.958 / 百合 0.917 / 櫻花 1 / 芙蓉 0.895 / 向日葵 0.929 / 滿天星 1。

---------------------------------------------------------------------------
**核心算法**
---------------------------------------------------------------------------
某班**舊生** = 該班**在籍學生**中 ``enrollment_school_year < cycle.academic_year``
者（民國學年；新生 == academic_year、未來年 > academic_year 皆不算舊生）。

    舊生率 = 舊生數 / **編制**(ClassEnrollmentTarget.head_count_target)

寫入 ``ClassEnrollmentTarget.returning_student_rate``（小數 3 位，如 0.917；
天堂鳥 0.917×24 編制 ≈ 22 舊生）。**分母是編制 head_count_target，不是在籍總數！**
（22 舊生 / 24 編制 = 0.917；若誤用在籍總數 22 當分母會得 1.000）。

對每個 ClassEnrollmentTarget（含 semester_first True/False 兩列，若存在）各算各寫。
同基準日 → 兩列同值（預期，鏡像 refresh_enrollment_rates 對 OrgYearSettings 的處理）。

**在籍基準日**：``cycle.bonus_calc_date``（對齊 settlement_builder.refresh_enrollment_rates
對 OrgYearSettings 全校達成率所用基準）。**在籍判定**：``enrollment_rates._enrolled_on_filter``
（純日期：enrollment_date <= d AND graduation_date >= d AND withdrawal_date > d，
不依賴 lifecycle_status 現態），與 count_enrolled_on / class_performance_rate 一致。

**class 歸屬**：採基準日「現態」classroom_id（point-in-time 單一快照），不走
classroom_at_month_end 的轉班歷史感知——returning rate 是單一基準日快照，現態
classroom_id 即為該日所屬班；不需 class_performance_rate 那種 6 月底逐月歷史。

---------------------------------------------------------------------------
**override / graceful fallback（業主決策）**
---------------------------------------------------------------------------
returning_student_rate 是 plain column（**無 source_ref 機制**，與 B2/B3 的
special_bonus_items 不同）。故：
  - **完整班**（無 NULL）→ **無條件覆寫** returning_student_rate（這正是把
    Phase 1 手填改 Phase 2 自動的預期行為，非 skip 手動筆）。
  - **fallback**：若某班**任一在籍學生** ``enrollment_school_year IS NULL``
    （prod backfill 未完成）→ **不寫該班**（沿用既有手填值，不寫半套）+
    ``report.fallback_classes += 1``。NULL 檢查只對**在籍集**（已退學 NULL 生
    不在籍 → 不觸發 fallback、不計分子）。
  - head_count_target <= 0 → 除零保護，不寫，記 fallback。

---------------------------------------------------------------------------
**全校舊生率 helper（給 B7 畢業班老師用）**
---------------------------------------------------------------------------
``school_wide_returning_rate(db, cycle) -> Decimal | None``：全校在籍學生中
``enrollment_school_year < academic_year`` 數 / 全校目標
（OrgYearSettings(semester_first=True).enrollment_target）。回 fraction（0.xxx，
與 column 同語意，B7 ×100 可直接用）。回 None 若：全校任一在籍學生
enrollment_school_year NULL（None-safe）/ OrgYearSettings 列缺 / enrollment_target<=0。

B7 會用此 helper：gather_performance_rates 對「有帶班角色但查不到對應
ClassEnrollmentTarget（班級已畢業/班導已離職）」的老師，class_returning_rate
用全校率而非 None（Excel footnote：「114 年 7 月畢業班老師(6 位)舊生註冊率依
全校舊生註冊率為主」）。B6 只提供 helper，wiring 在 B7（**不動 settlement_builder**）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from models.classroom import Student
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    YearEndCycle,
)
from services.year_end.enrollment_rates import _enrolled_on_filter

logger = logging.getLogger(__name__)

_Q3 = Decimal("0.001")  # returning_student_rate 為 Numeric(6,3)


def _q3(x) -> Decimal:
    """四捨五入至小數點後三位（ROUND_HALF_UP）；本模組自帶以保持 auto_derive 自含。"""
    return Decimal(str(x)).quantize(_Q3, rounding=ROUND_HALF_UP)


@dataclass
class ReturningRateReport:
    """⑥ 班級舊生率推導結果。

    written          : 寫入/覆寫的 ClassEnrollmentTarget 列數
    fallback_classes : 因在籍學生 enrollment_school_year NULL（或編制<=0）而跳過的列數
    school_wide_rate : 全校舊生率（fraction，0.xxx）；無法計算回 None
    warnings         : 略過原因
    """

    written: int = 0
    fallback_classes: int = 0
    school_wide_rate: Optional[Decimal] = None
    warnings: list[str] = field(default_factory=list)


def _class_counts(db: Session, classroom_id: int, basis_date, academic_year: int):
    """回該班在基準日的三個在籍計數：(total, null_count, old_count)。

    在籍集 = _enrolled_on_filter(basis_date) AND classroom_id == 該班（現態）。
      total     : 在籍總數
      null_count: 在籍且 enrollment_school_year IS NULL（觸發 fallback）
      old_count : 在籍且 enrollment_school_year < academic_year（舊生，分子）

    以單次 query 三條 conditional aggregate，避免拉全部 row。
    """
    base_filter = (Student.classroom_id == classroom_id) & _enrolled_on_filter(
        basis_date
    )
    row = db.execute(
        select(
            func.count(Student.id),
            func.sum(func.cast(Student.enrollment_school_year.is_(None), Integer)),
            func.sum(
                func.cast(Student.enrollment_school_year < academic_year, Integer)
            ),
        ).where(base_filter)
    ).one()
    total = int(row[0] or 0)
    null_count = int(row[1] or 0)
    old_count = int(row[2] or 0)
    return total, null_count, old_count


def derive_returning_rate(db: Session, cycle: YearEndCycle) -> ReturningRateReport:
    """推導 ⑥ 班級舊生率 → 覆寫 ClassEnrollmentTarget.returning_student_rate。

    只 flush（由呼叫端 commit）。idempotent。

    完整班無條件覆寫（Phase1 手填 → Phase2 自動）；在籍學生有 NULL
    enrollment_school_year → 不寫該班、保留既有手填、fallback_classes += 1。
    順帶算全校率（school_wide_returning_rate）填入 report 供觀測。
    """
    report = ReturningRateReport()
    basis_date = cycle.bonus_calc_date
    academic_year = cycle.academic_year

    targets = list(
        db.scalars(
            select(ClassEnrollmentTarget).where(
                ClassEnrollmentTarget.year_end_cycle_id == cycle.id
            )
        )
    )

    for tgt in targets:
        target_count = int(tgt.head_count_target or 0)
        if target_count <= 0:
            report.fallback_classes += 1
            report.warnings.append(
                f"班 classroom_id={tgt.classroom_id} "
                f"(semester_first={tgt.semester_first}) 編制<=0，跳過（保留既有值）"
            )
            continue

        total, null_count, old_count = _class_counts(
            db, tgt.classroom_id, basis_date, academic_year
        )

        # graceful fallback（優先）：基準日 0 在籍（時序假象，如開學前快照或全班已退）→
        # 不寫 0.000 覆蓋手填；保留既有值，記 fallback。
        if total <= 0:
            report.fallback_classes += 1
            report.warnings.append(
                f"班 classroom_id={tgt.classroom_id} "
                f"(semester_first={tgt.semester_first}) 基準日 0 在籍（時序假象），"
                f"跳過（保留既有值）"
            )
            continue

        # graceful fallback：在籍集有任一 NULL → 不寫半套，保留既有手填。
        if null_count > 0:
            report.fallback_classes += 1
            report.warnings.append(
                f"班 classroom_id={tgt.classroom_id} "
                f"(semester_first={tgt.semester_first}) 有 {null_count} 名在籍學生 "
                f"enrollment_school_year 未回填，跳過（保留既有值）"
            )
            continue

        # 完整班：無條件覆寫（plain column，無 source_ref 機制）。
        rate = _q3(Decimal(old_count) / Decimal(target_count))
        tgt.returning_student_rate = rate
        report.written += 1

    # 全校率（供觀測 + B7 helper 一致性）；不依賴上面迴圈，獨立計算。
    report.school_wide_rate = school_wide_returning_rate(db, cycle)

    db.flush()
    logger.info(
        "returning_rate derive: cycle=%s written=%d fallback=%d school_wide=%s",
        cycle.academic_year,
        report.written,
        report.fallback_classes,
        report.school_wide_rate,
    )
    return report


def school_wide_returning_rate(db: Session, cycle: YearEndCycle) -> Optional[Decimal]:
    """全校舊生率（fraction，0.xxx，3dp）；無法計算回 None。

    = 全校在籍學生中 enrollment_school_year < academic_year 數 / 全校目標
      (OrgYearSettings(semester_first=True).enrollment_target)。

    回 None 若：
      - OrgYearSettings(semester_first=True) 列缺
      - enrollment_target <= 0（除零保護）
      - 全校任一**在籍**學生 enrollment_school_year IS NULL（None-safe，不寫半套）

    基準日 = cycle.bonus_calc_date；在籍判定 = _enrolled_on_filter（純日期）。
    NULL 檢查只對在籍集（已退學 NULL 生不在籍 → 不阻斷、不計分子）。
    回 fraction（與 ClassEnrollmentTarget.returning_student_rate 同語意，B7 ×100 可直接用）。
    """
    org = db.scalar(
        select(OrgYearSettings).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first.is_(True),
        )
    )
    if org is None:
        return None

    target = int(org.enrollment_target or 0)
    if target <= 0:
        return None

    basis_date = cycle.bonus_calc_date
    academic_year = cycle.academic_year

    row = db.execute(
        select(
            func.count(Student.id),
            func.sum(func.cast(Student.enrollment_school_year.is_(None), Integer)),
            func.sum(
                func.cast(Student.enrollment_school_year < academic_year, Integer)
            ),
        ).where(_enrolled_on_filter(basis_date))
    ).one()
    total = int(row[0] or 0)
    null_count = int(row[1] or 0)
    old_count = int(row[2] or 0)

    # 全校基準日 0 在籍（時序假象）→ 回 None（與 None-safe 路徑一致，B7 fallback 正確）。
    if total <= 0:
        return None

    if null_count > 0:
        return None

    return _q3(Decimal(old_count) / Decimal(target))
