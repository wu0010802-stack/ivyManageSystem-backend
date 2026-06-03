"""年終獎金 E化 階段2 — auto-derive 子套件。

各特別獎金欄位的「自動推導」邏輯，每個 derive 函式各自一檔、單一責任：
  - after_class_award.py    : ① 才藝鼓勵（報名人次 × 班別單價）→ AFTER_CLASS_AWARD
  - festival_diff.py        : ③ 節慶差額（應領 − 已發）→ FESTIVAL_DIFF
  - semester_dividend.py    : ④ 學期紅利（舊生率/才藝率達標加給）→ SEMESTER_DIVIDEND_*
  - attendance_deductions.py: ⑤a 考勤扣款（純計算，不寫 settlement；B7 wire 進 gather_deductions）
  - returning_rate.py       : ⑥ 班級舊生率（寫 ClassEnrollmentTarget.returning_student_rate）

編排層 ``derive_all`` 由 B7 補上（見下），呼叫 ① ③ ④ ⑥（寫 special_bonus_items /
returning_rate）並彙總各 report；⑤a **不在** derive_all（per-employee 純計算，由 B7 在
settlement_builder.gather_deductions wiring）。

override 慣例（橫跨 B2-B7）：auto-derive 寫入的 special_bonus_items 一律以
``source_ref`` 前綴 ``"auto:"`` 標記為「自動」。upsert 時：
  1. 以 uq 鍵 (year_end_cycle_id, employee_id, bonus_type, period_label) 查既有 row。
  2. 既有 row 的 source_ref **不是** ``auto:`` 開頭（None 或使用者手填）→ 手動筆，SKIP。
  3. 既有 row 的 source_ref 以 ``auto:`` 開頭 → 上次自動寫的，UPDATE。
  4. 不存在 → 新建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from models.year_end import YearEndCycle
from services.year_end.auto_derive.after_class_award import derive_after_class_award
from services.year_end.auto_derive.festival_diff import derive_festival_diff
from services.year_end.auto_derive.returning_rate import derive_returning_rate
from services.year_end.auto_derive.semester_dividend import derive_semester_dividend

logger = logging.getLogger(__name__)


@dataclass
class DeriveReport:
    """derive_all 彙整結果（供 build_settlements 帶回 BuildResult.derive_report、grid/試算提醒用）。

    unmatched_count  : ① 才藝鼓勵未配對報名人次（AcaReport.unmatched_count）。
                       > 0 表示有報名因班級未配對而未計入任何班導獎金，HR 需處理。
    fallback_classes : ⑥ 班級舊生率因在籍學生 enrollment_school_year 未回填（或編制<=0）
                       而沿用既有手填值（未自動覆寫）的班數（ReturningRateReport.fallback_classes）。
    written          : 四個 derive 寫入/更新筆數合計（觀測用，不含 skip 的手動筆）。
    skipped_manual   : 四個 derive 因手動筆而 skip 的合計（觀測用）。
    warnings         : 各 derive warnings 串接（缺單價/缺班導/缺目標等）。
    """

    unmatched_count: int = 0
    fallback_classes: int = 0
    written: int = 0
    skipped_manual: int = 0
    warnings: list[str] = field(default_factory=list)


def derive_all(db: Session, cycle: YearEndCycle) -> DeriveReport:
    """編排 ① ③ ④ ⑥ 四個 auto-derive，彙整成單一 DeriveReport。

    順序（**⑥ 必須在 ④ 之前**）：
      1. ⑥ derive_returning_rate  → 寫 ClassEnrollmentTarget.returning_student_rate
      2. ① derive_after_class_award→ AFTER_CLASS_AWARD（unmatched_count 來源）
      3. ③ derive_festival_diff   → FESTIVAL_DIFF
      4. ④ derive_semester_dividend→ SEMESTER_DIVIDEND_*（**讀** returning_student_rate，
         故必排在 ⑥ 之後，否則讀到舊/空的舊生率）
    ① ③ 順序不拘（彼此獨立，皆寫 special_bonus_items 不同 bonus_type）。

    ⑤a 考勤扣款**不在此**：它是 per-employee 純計算，由 B7 在 gather_deductions wiring。

    split-brain 註記（③ festival_diff）：derive_festival_diff 內部建
    ``SalaryEngine(load_from_db=True)``，會**自開 session** 讀 BonusConfig/級距等
    reference data。故 derive_all 的呼叫端（build_settlements refresh 階段）必須確保
    BonusConfig / cycle / ClassEnrollmentTarget / OrgYearSettings 等 reference data
    在呼叫前已是 **committed 或對另一 session 可見** 的一致狀態——正常 HR 流程中 config
    早於 build 已 committed（PUT /config/* → commit），故無虞；但若同交易內先改了
    BonusConfig 再 build，engine 自開的 session 會讀到舊值（須先 commit）。

    本函式只 flush（各 derive 內部 flush）；由呼叫端 commit。idempotent。
    """
    report = DeriveReport()

    # 1. ⑥ 班級舊生率（必須先於 ④，④ 讀 returning_student_rate）
    ret = derive_returning_rate(db, cycle)
    report.fallback_classes += ret.fallback_classes
    report.written += ret.written
    report.warnings.extend(ret.warnings)

    # 2. ① 才藝鼓勵（unmatched_count 來源）
    aca = derive_after_class_award(db, cycle)
    report.unmatched_count += aca.unmatched_count
    report.written += aca.written
    report.skipped_manual += aca.skipped_manual
    report.warnings.extend(aca.warnings)

    # 3. ③ 節慶差額（①/③ 順序不拘）
    fd = derive_festival_diff(db, cycle)
    report.written += fd.written
    report.skipped_manual += fd.skipped_manual
    report.warnings.extend(fd.warnings)

    # 4. ④ 學期紅利（讀 ⑥ 寫入的 returning_student_rate）
    sd = derive_semester_dividend(db, cycle)
    report.written += sd.written
    report.skipped_manual += sd.skipped_manual
    report.warnings.extend(sd.warnings)

    logger.info(
        "derive_all: cycle=%s unmatched=%d fallback_classes=%d written=%d skipped_manual=%d",
        cycle.academic_year,
        report.unmatched_count,
        report.fallback_classes,
        report.written,
        report.skipped_manual,
    )
    return report


__all__ = [
    "DeriveReport",
    "derive_all",
    "derive_after_class_award",
    "derive_festival_diff",
    "derive_returning_rate",
    "derive_semester_dividend",
]
