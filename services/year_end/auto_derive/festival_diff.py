"""B3 ③ 節慶差額（FESTIVAL_DIFF）自動推導。

Excel「年終獎金總表」FESTIVAL_DIFF：N.8 ~ N+1.01 共 6 個月（上學期），
逐月「應領 − 已發」加總，多退少補（**可為負**）：

  應領_m = festival_base_for_role(role) × (在園_m / 目標)
    - 班導 → 在園 = count_enrolled_on(db, d, classroom_id=該班)
            目標 = 該班 ClassEnrollmentTarget(semester_first=True).head_count_target
    - 非帶班 → 在園 = count_enrolled_on(db, d)（全校）
            目標 = OrgYearSettings(semester_first=True).enrollment_target
  已發_m = SalaryRecord(salary_year=d.year, salary_month=d.month).festival_bonus（無→0）
  差額_m = 應領_m − 已發_m

month-end 日期 d 由 _semester_month_ends(cycle, semester_first=True) 提供，
回傳的已是西元曆日期（AY114 → 2025-08-31 … 2026-01-31），故 SalaryRecord 查詢
直接用 d.year / d.month（SalaryRecord.salary_year 為西元；已對既有 engine.py
建立處與 salary 測試確認）。

班導 vs 非帶班判定：mirror settlement_builder.gather_performance_rates —
以「ClassEnrollmentTarget(year_end_cycle_id, semester_first=True,
head_teacher_employee_id==emp.id) 是否存在」為唯一判定（不前置 _has_class_role
避免出現「有帶班角色但無 target row」的未定義分支）。

參與者範圍：在職（is_active）且 festival 基數 > 0 的員工。
**刻意排除 festival 基數 = 0 的角色**（廚房/護理/美語/無法分類者）：對這些人
應領_m 恆為 0，若 payroll 仍發 festival_bonus，差額會變成「全負」清空已合法發放的
節慶獎金——那不是「多退少補（比例 true-up）」的語意，而是「本不該發」的回收，
非本欄職責。controller 待確認：是否需對 festival 基數=0 但已發 festival 的員工
產生負 FESTIVAL_DIFF（見 task report）。

override 慣例見 auto_derive/__init__.py：source_ref 以 ``auto:`` 標記自動筆；
手動筆（source_ref 非 auto: 開頭）絕不覆寫。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.employee import Employee
from models.salary import SalaryRecord
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from services.year_end.enrollment_rates import count_enrolled_on
from services.year_end.settlement_builder import (
    _semester_month_ends,
    festival_base_for_role,
    role_key_of,
)
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

_SOURCE_REF = "auto:festival_diff"
_Q2 = Decimal("0.01")


def _q2(x) -> Decimal:
    """四捨五入至小數點後兩位（ROUND_HALF_UP）；本模組自帶以保持 auto_derive 自含。"""
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def period_label(cycle: YearEndCycle) -> str:
    """穩定的 upsert period_label（每員工每 cycle 一筆 FESTIVAL_DIFF）。"""
    return f"{cycle.academic_year}-FD"


@dataclass
class FestivalDiffReport:
    """③ 節慶差額推導結果。

    written        : 寫入/更新的 SpecialBonusItem 筆數（不含 skip 的手動筆）
    skipped_manual : 因手動筆而 skip 的員工數
    warnings       : 略過原因（缺全校目標等）
    """

    written: int = 0
    skipped_manual: int = 0
    warnings: list[str] = field(default_factory=list)


def _upsert_auto_item(
    db: Session,
    *,
    cycle_id: int,
    employee_id: int,
    label: str,
    amount: Decimal,
    classroom_id: Optional[int],
    calc_meta: dict,
) -> bool:
    """override-aware upsert（與 B2 _upsert_auto_item 等價，bonus_type=FESTIVAL_DIFF）。

    回傳 True 表示有寫入/更新（新建或更新自動筆）；
    回傳 False 表示既有筆為手動筆而 SKIP（絕不覆寫）。
    """
    existing = db.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle_id,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type == SpecialBonusType.FESTIVAL_DIFF,
            SpecialBonusItem.period_label == label,
        )
    )
    if existing is None:
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle_id,
                employee_id=employee_id,
                bonus_type=SpecialBonusType.FESTIVAL_DIFF,
                period_label=label,
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


def _head_teacher_target(
    db: Session, cycle: YearEndCycle, emp: Employee
) -> Optional[ClassEnrollmentTarget]:
    """emp 在本 cycle 上學期是否為班導 → 回該班 ClassEnrollmentTarget，否則 None。

    mirror settlement_builder.gather_performance_rates 的判定查詢（唯一判定）。
    """
    return db.scalar(
        select(ClassEnrollmentTarget).where(
            ClassEnrollmentTarget.year_end_cycle_id == cycle.id,
            ClassEnrollmentTarget.semester_first.is_(True),
            ClassEnrollmentTarget.head_teacher_employee_id == emp.id,
        )
    )


def _paid_festival(db: Session, employee_id: int, d: date) -> Decimal:
    """已發 festival_bonus（SalaryRecord，salary_year=d.year / salary_month=d.month）。"""
    raw = db.scalar(
        select(SalaryRecord.festival_bonus).where(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == d.year,
            SalaryRecord.salary_month == d.month,
        )
    )
    return Decimal(str(raw)) if raw is not None else Decimal("0")


def derive_festival_diff(db: Session, cycle: YearEndCycle) -> FestivalDiffReport:
    """推導 ③ 節慶差額 → upsert special_bonus_items（FESTIVAL_DIFF）。

    只 flush（由呼叫端 commit）。idempotent；手動筆不覆寫（見 __init__ override 慣例）。
    參與者 = 在職且 festival 基數 > 0 的員工。
    """
    report = FestivalDiffReport()
    label = period_label(cycle)
    month_ends = _semester_month_ends(cycle, semester_first=True)

    # 全校目標（上學期 OrgYearSettings）— 非帶班員工用
    org = db.scalar(
        select(OrgYearSettings).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first.is_(True),
        )
    )
    school_target = org.enrollment_target if org is not None else None

    employees = list(
        db.scalars(select(Employee).where(Employee.is_active.is_(True))).all()
    )

    for emp in employees:
        festival_base = festival_base_for_role(db, role_key_of(emp))
        # festival 基數 = 0 的角色（廚房/護理/美語/無法分類）刻意排除，避免「全負回收」
        if festival_base <= 0:
            continue

        target_row = _head_teacher_target(db, cycle, emp)
        if target_row is not None:
            classroom_id: Optional[int] = target_row.classroom_id
            target = target_row.head_count_target
        else:
            classroom_id = None
            target = school_target
            if target is None:
                report.warnings.append(
                    f"員工 {emp.id} 非帶班但無全校目標(OrgYearSettings)，略過"
                )
                continue

        if target is None or int(target) <= 0:
            report.warnings.append(f"員工 {emp.id} 目標人數 <= 0，略過")
            continue

        target_d = Decimal(str(target))
        total_diff = Decimal("0")
        months_meta: list[dict] = []
        for d in month_ends:
            enrolled = count_enrolled_on(db, d, classroom_id=classroom_id)
            due = _q2(festival_base * Decimal(enrolled) / target_d)
            paid = _q2(_paid_festival(db, emp.id, d))
            diff = due - paid
            total_diff += diff
            months_meta.append(
                {
                    "month": f"{d.year}-{d.month:02d}",
                    "enrolled": enrolled,
                    "target": int(target),
                    "due": str(due),
                    "paid": str(paid),
                    "diff": str(diff),
                }
            )

        amount = _q2(total_diff)
        wrote = _upsert_auto_item(
            db,
            cycle_id=cycle.id,
            employee_id=emp.id,
            label=label,
            amount=amount,
            classroom_id=classroom_id,
            calc_meta={
                "festival_base": str(_q2(festival_base)),
                "is_head_teacher": classroom_id is not None,
                "months": months_meta,
            },
        )
        if wrote:
            report.written += 1
        else:
            report.skipped_manual += 1

    db.flush()
    logger.info(
        "festival_diff derive: cycle=%s written=%d skipped_manual=%d",
        cycle.academic_year,
        report.written,
        report.skipped_manual,
    )
    return report
