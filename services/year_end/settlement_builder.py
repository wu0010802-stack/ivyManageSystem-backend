"""services/year_end/settlement_builder.py — 年終結算 builder helpers（階段1）。

決策④：節慶=角色基數查表（BonusConfig 最新 is_active 列），非全年加總。

此模組只提供三個純/純ish helper：
  - festival_base_for_role   : 依角色查節慶獎金基數
  - compute_hire_months      : 計算在職月數（整個 cycle 或部分）
  - resolve_org_achievement_rate : 解析組織績效達成率（滿年平均 / 僅在職學期）

build_settlements 編排邏輯在後續 Task 3 實作，本模組不含。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from models.config import BonusConfig

# --------------------------------------------------------------------------- #
# 精度常數                                                                     #
# --------------------------------------------------------------------------- #

_Q2 = Decimal("0.01")  # 金額，小數 2 位
_Q1 = Decimal("0.1")  # 達成率，小數 1 位


def _q2(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _q1(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(_Q1, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# 角色 key → BonusConfig 節慶基數欄位名稱對應表                               #
# --------------------------------------------------------------------------- #

_FESTIVAL_FIELD: dict[str, str] = {
    "head_teacher_ab": "head_teacher_ab",
    "head_teacher_c": "head_teacher_c",
    "assistant_teacher_ab": "assistant_teacher_ab",
    "assistant_teacher_c": "assistant_teacher_c",
    "principal": "principal_festival",
    "director": "director_festival",
    "leader": "leader_festival",
    "driver": "driver_festival",
    "designer": "designer_festival",
    "admin": "admin_festival",
    "art_teacher": "art_teacher_festival",
}


# --------------------------------------------------------------------------- #
# Helper 1：節慶獎金角色基數查表                                               #
# --------------------------------------------------------------------------- #


def festival_base_for_role(db: Session, role_key: "str | None") -> Decimal:
    """查最新 BonusConfig（is_active + id DESC）取角色對應節慶基數。

    Args:
        db: SQLAlchemy session。
        role_key: 角色識別鍵，須存在於 _FESTIVAL_FIELD 對應表中。

    Returns:
        Decimal（小數 2 位）；查無設定或 role_key 不在對應表時回 Decimal("0")。
    """
    field_name = _FESTIVAL_FIELD.get(role_key)
    if field_name is None:
        return Decimal("0")

    config: BonusConfig | None = (
        db.query(BonusConfig)
        .filter(BonusConfig.is_active == True)  # noqa: E712
        .order_by(BonusConfig.id.desc())
        .first()
    )
    if config is None:
        return Decimal("0")

    raw = getattr(config, field_name, None)
    if raw is None:
        return Decimal("0")

    return _q2(raw)


# --------------------------------------------------------------------------- #
# Helper 2：在職月數計算                                                       #
# --------------------------------------------------------------------------- #


def compute_hire_months(emp: Any, cycle_start: date, cycle_end: date) -> Decimal:
    """計算員工在 [cycle_start, cycle_end] 週期內的在職月數。

    月數採 calendar-month inclusive 計算（只看年/月，忽略日；start.m 到 end.m 均算一個月）。
    結果 clamp 在 [0, 12]。

    Args:
        emp: 任意有 hire_date / resign_date 屬性的物件（None 表示無限制）。
        cycle_start: 週期開始日。
        cycle_end:   週期結束日。

    Returns:
        Decimal（整數），在職月數。
    """
    hire_date: date | None = getattr(emp, "hire_date", None)
    resign_date: date | None = getattr(emp, "resign_date", None)

    # 將 None 視為「週期邊界」
    effective_start = (
        max(hire_date, cycle_start) if hire_date is not None else cycle_start
    )
    effective_end = (
        min(resign_date, cycle_end) if resign_date is not None else cycle_end
    )

    if effective_end < effective_start:
        return Decimal("0")

    months = (
        (effective_end.year - effective_start.year) * 12
        + (effective_end.month - effective_start.month)
        + 1
    )

    # clamp 0..12
    months = max(0, min(12, months))
    return Decimal(str(months))


# --------------------------------------------------------------------------- #
# Helper 3：組織績效達成率解析                                                 #
# --------------------------------------------------------------------------- #


def resolve_org_achievement_rate(
    first: Any,
    second: Any,
    *,
    worked_first: bool,
    worked_second: bool,
) -> Decimal:
    """解析組織績效達成率。

    - 滿年（worked_first=True, worked_second=True）：兩學期平均（四捨五入小數 1 位）。
    - 只在職一學期：直接取該學期的達成率。
    - 兩者皆 False（異常資料）：回 Decimal("0.0")。

    Args:
        first:         第一學期達成率（數值或 Decimal）。
        second:        第二學期達成率（數值或 Decimal）。
        worked_first:  是否在第一學期在職。
        worked_second: 是否在第二學期在職。

    Returns:
        Decimal（小數 1 位）。
    """
    rates: list[Decimal] = []
    if worked_first and first is not None:
        rates.append(Decimal(str(first)))
    if worked_second and second is not None:
        rates.append(Decimal(str(second)))

    if not rates:
        return Decimal("0.0")

    average = sum(rates) / len(rates)
    return _q1(average)


# =========================================================================== #
# Task 3：build_settlements 跨員工編排 + upsert                               #
# =========================================================================== #

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text

if TYPE_CHECKING:
    from services.year_end.auto_derive import DeriveReport

from models.employee import Employee
from models.year_end import (
    ClassEnrollmentTarget,
    EmployeeYearEndSnapshot,
    OrgYearSettings,
    SpecialBonusItem,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.salary.engine import SalaryEngine, load_position_salary_standards
from services.year_end import enrollment_rates
from services.year_end.engine import (
    DeductionBreakdown,
    PerformanceRates,
    compute_settlement,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 角色 key 解析（與 SalaryEngine._resolve_standard_base 的分流一致）          #
# --------------------------------------------------------------------------- #


def _bonus_grade_of(emp: Any) -> str:
    """決定節慶等級 a/b/c，邏輯對齊 _resolve_standard_base。"""
    title = (
        emp.job_title_rel.name
        if getattr(emp, "job_title_rel", None)
        else (getattr(emp, "title", None) or "")
    )
    bonus_grade = getattr(emp, "bonus_grade", None)
    if bonus_grade and bonus_grade.lower() in ("a", "b", "c"):
        return bonus_grade.lower()
    if title == "幼兒園教師":
        return "a"
    if title in ("教保員", "助理教保員"):
        return "b"
    return "c"


def role_key_of(emp: Any) -> "str | None":
    """員工 → 節慶獎金角色 key（_FESTIVAL_FIELD 之 key），或 None（無節慶基數）。

    與 services/salary/engine._resolve_standard_base 的職位分流保持一致，
    確保節慶基數查表（festival_base_for_role）與底薪解析用同一套角色判定。

    班導/副班導以 ab（a 或 b 等級合併）與 c 兩檔；領導職、行政、司機、
    美師、美編各自對應。廚房/護理/美語等無對應節慶欄位的角色，以及任何
    無法分類者，回 None → festival_base_for_role 自動回 Decimal("0")。

    # TODO(phase1.5): 理想上復用月薪 festival 模組的 role→base 對應以完全一致
    """
    title = (
        emp.job_title_rel.name
        if getattr(emp, "job_title_rel", None)
        else (getattr(emp, "title", None) or "")
    )
    position = getattr(emp, "position", None) or ""

    # 領導職優先
    if position == "主任" or title == "主任":
        return "director"
    if position == "園長" or title == "園長":
        return "principal"

    # 職稱關鍵字分流（順序與 _resolve_standard_base 一致）
    if "司機" in title:
        return "driver"
    if "廚" in title:
        # 廚房無對應節慶欄位 → 節慶基數 0（對齊 Excel 廚工=0，不以 admin 兜底）
        return None
    if "美師" in title or "藝術" in title:
        return "art_teacher"
    if "美編" in title or "設計" in title:
        return "designer"
    if position == "行政":
        return "admin"

    grade = _bonus_grade_of(emp)
    ab = "ab" if grade in ("a", "b") else "c"
    if position in ("班導", "班導師") or (title == "組長" and position == "班導"):
        return f"head_teacher_{ab}"
    if position in ("副班導", "副班導師"):
        return f"assistant_teacher_{ab}"
    return None


def _has_class_role(emp: Any) -> bool:
    """是否為帶班角色（班導/副班導）— 決定 class_* 績效是否參與平均。"""
    rk = role_key_of(emp)
    return rk is not None and (
        rk.startswith("head_teacher_") or rk.startswith("assistant_teacher_")
    )


# --------------------------------------------------------------------------- #
# base_salary：複用月薪引擎的底薪解析（決策①A）                              #
# --------------------------------------------------------------------------- #


def year_end_base_salary(
    db: Session, emp: Any, standards: dict | None = None
) -> Decimal:
    """年終底薪 == 月薪底薪（決策①A）。

    複用 SalaryEngine._resolve_standard_base，但**用傳入的 db** 載入職位標準
    （load_position_salary_standards(db)），避免 engine.load_config_from_db()
    自開 session 讀到別的 DB。流程：
      1. 建最小 SalaryEngine(load_from_db=False)（不觸 DB）。
      2. 從傳入 db 載 _position_salary_standards。
      3. 呼叫 _resolve_standard_base(emp) — 有職位標準回標準薪；園長/主任等
         無對應或 bypass_standard_base 回 emp.base_salary；時薪制回 0。

    回 Decimal（小數 2 位）。
    """
    engine = SalaryEngine(load_from_db=False)
    engine._position_salary_standards = (
        standards if standards is not None else load_position_salary_standards(db)
    )
    resolved = engine._resolve_standard_base(emp)
    return _q2(resolved)


# --------------------------------------------------------------------------- #
# 學期區間 / 在職判定                                                         #
# --------------------------------------------------------------------------- #


def _semester_ranges(
    cycle: YearEndCycle,
) -> tuple[tuple[date, date], tuple[date, date]]:
    """回 (上學期, 下學期) 的 (start, end) 區間。

    學年 N（民國）= 西元 N+1911 年 8 月 ～ N+1912 年 7 月。
    上學期(semester_first=True)：8 月～次年 1 月；下學期：2 月～7 月。
    以 cycle.start_date 的西元年推算（cycle.start_date 通常為 N+1911 年 8/1）。
    """
    start_year = cycle.start_date.year  # 通常 = N+1911
    first = (date(start_year, 8, 1), date(start_year + 1, 1, 31))
    second = (date(start_year + 1, 2, 1), date(start_year + 1, 7, 31))
    return first, second


def _semester_month_ends(cycle: YearEndCycle, semester_first: bool) -> list[date]:
    """回該學期 6 個月底日期（用於 class_performance_rate）。

    上學期：8,9,10,11,12,1 月底；下學期：2,3,4,5,6,7 月底。
    """
    import calendar

    (f_start, _), (s_start, _) = _semester_ranges(cycle)
    base = f_start if semester_first else s_start
    months: list[date] = []
    y, m = base.year, base.month
    for _ in range(6):
        last_day = calendar.monthrange(y, m)[1]
        months.append(date(y, m, last_day))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def worked_semesters(emp: Any, cycle: YearEndCycle) -> tuple[bool, bool]:
    """員工是否在上學期 / 下學期在職（依 hire_date/resign_date 與學期區間重疊判定）。"""
    (f_start, f_end), (s_start, s_end) = _semester_ranges(cycle)
    hire = getattr(emp, "hire_date", None)
    resign = getattr(emp, "resign_date", None)

    def overlaps(seg_start: date, seg_end: date) -> bool:
        eff_start = max(hire, seg_start) if hire is not None else seg_start
        eff_end = min(resign, seg_end) if resign is not None else seg_end
        return eff_end >= eff_start

    return overlaps(f_start, f_end), overlaps(s_start, s_end)


# --------------------------------------------------------------------------- #
# (A) refresh_enrollment_rates：由在籍資料回填 stored rates                   #
# --------------------------------------------------------------------------- #


def refresh_enrollment_rates(db: Session, cycle: YearEndCycle) -> None:
    """由在籍資料計算並寫回 OrgYearSettings / ClassEnrollmentTarget 的 stored rates。

    - OrgYearSettings（兩列，semester_first True/False）：
        school_achievement_rate = enrollment_rates.school_achievement_rate(
            db, basis_date, enrollment_target)
        並同步寫 enrollment_actual。
    - ClassEnrollmentTarget（每列）：
        class_performance_rate = enrollment_rates.class_performance_rate(
            db, classroom_id, month_ends, head_count_target)
        並同步寫 avg_monthly_enrollment。
    - returning_student_rate（班舊生率）為 Phase 1 人工維護，本函式不動。
    """
    # OrgYearSettings：全校達成率
    org_rows = db.scalars(
        select(OrgYearSettings).where(OrgYearSettings.year_end_cycle_id == cycle.id)
    ).all()
    for org in org_rows:
        # TODO(phase2): per-semester basis date；目前 OrgYearSettings 未存學期基準日，
        # 統一用 cycle.bonus_calc_date（兩學期相同），不過度設計。
        basis_date = cycle.bonus_calc_date
        actual = enrollment_rates.count_enrolled_on(db, basis_date)
        org.enrollment_actual = actual
        org.school_achievement_rate = enrollment_rates.school_achievement_rate(
            db, basis_date, org.enrollment_target
        )

    # ClassEnrollmentTarget：班級經營績效
    cls_rows = db.scalars(
        select(ClassEnrollmentTarget).where(
            ClassEnrollmentTarget.year_end_cycle_id == cycle.id
        )
    ).all()
    for cls in cls_rows:
        month_ends = _semester_month_ends(cycle, cls.semester_first)
        rate = enrollment_rates.class_performance_rate(
            db, cls.classroom_id, month_ends, cls.head_count_target
        )
        cls.class_performance_rate = rate
        # avg_monthly_enrollment = rate% × target / 100（由 rate 反推平均在籍）
        cls.avg_monthly_enrollment = _q2(
            rate * Decimal(cls.head_count_target) / Decimal("100")
        )

    db.flush()


# --------------------------------------------------------------------------- #
# (B) gather helpers：薄查詢                                                  #
# --------------------------------------------------------------------------- #


def gather_performance_rates(
    db: Session, cycle: YearEndCycle, emp: Any, *, school_rates=None
) -> PerformanceRates:
    """讀取 STORED rates 組 PerformanceRates（百分比）。

    - school_rate_first/second   ← OrgYearSettings.school_achievement_rate（兩學期）
    - class_performance_rate_*    ← ClassEnrollmentTarget.class_performance_rate
                                    （head_teacher_employee_id == emp.id 的那班；無→None）
    - class_returning_rate_*      ← ClassEnrollmentTarget.returning_student_rate × 100
    無帶班角色（STAFF/COOK/admin/領導職）→ class_* 全 None（引擎僅以全校率平均）。
    """
    # 全校達成率（兩學期）只依 cycle、與員工無關：caller（build_settlements 迴圈）以
    # school_rates 預先查一次傳入，避免 per-employee 2N 重查；未傳則自行查（standalone）。
    if school_rates is not None:
        school_first, school_second = school_rates
    else:
        org_first = db.scalar(
            select(OrgYearSettings).where(
                OrgYearSettings.year_end_cycle_id == cycle.id,
                OrgYearSettings.semester_first == True,  # noqa: E712
            )
        )
        org_second = db.scalar(
            select(OrgYearSettings).where(
                OrgYearSettings.year_end_cycle_id == cycle.id,
                OrgYearSettings.semester_first == False,  # noqa: E712
            )
        )
        school_first = (
            Decimal(str(org_first.school_achievement_rate)) if org_first else None
        )
        school_second = (
            Decimal(str(org_second.school_achievement_rate)) if org_second else None
        )

    class_perf_first = class_perf_second = None
    class_ret_first = class_ret_second = None

    if _has_class_role(emp):
        # B7 ⑥ 畢業班 fallback：班導有帶班角色但查不到對應 ClassEnrollmentTarget
        # （班級已畢業/班導已離職 → 無 target row）時，class_returning_rate 用「全校
        # 舊生率 × 100」而非 None（Excel footnote：114/07 畢業班老師舊生率依全校為主）。
        # 全校率只在「至少一學期無 target」時才需要 → 迴圈內 lazy 取一次（None 表已快取）。
        _school_wide_ret = _SENTINEL = object()
        for semester_first in (True, False):
            ct = db.scalar(
                select(ClassEnrollmentTarget).where(
                    ClassEnrollmentTarget.year_end_cycle_id == cycle.id,
                    ClassEnrollmentTarget.semester_first == semester_first,
                    ClassEnrollmentTarget.head_teacher_employee_id == emp.id,
                )
            )
            if ct is None:
                # 畢業班 fallback：class_returning_rate 用全校舊生率 ×100；
                # class_performance_rate 維持 None（無班無經營績效）。helper 回 None → 維持 None。
                if _school_wide_ret is _SENTINEL:
                    from services.year_end.auto_derive.returning_rate import (
                        school_wide_returning_rate,
                    )

                    _school_wide_ret = school_wide_returning_rate(db, cycle)
                if _school_wide_ret is not None:
                    ret_fb = _school_wide_ret * Decimal("100")
                    if semester_first:
                        class_ret_first = ret_fb
                    else:
                        class_ret_second = ret_fb
                continue
            perf = Decimal(str(ct.class_performance_rate))
            ret = Decimal(str(ct.returning_student_rate)) * Decimal("100")
            if semester_first:
                class_perf_first, class_ret_first = perf, ret
            else:
                class_perf_second, class_ret_second = perf, ret

    return PerformanceRates(
        school_rate_first=school_first,
        school_rate_second=school_second,
        class_returning_rate_first=class_ret_first,
        class_returning_rate_second=class_ret_second,
        class_performance_rate_first=class_perf_first,
        class_performance_rate_second=class_perf_second,
    )


def gather_deductions(db: Session, cycle: YearEndCycle, emp: Any) -> DeductionBreakdown:
    """組 DeductionBreakdown（B7 ⑤a wiring）。薄殼：查既有 settlement 後委派
    ``_deductions_from_settlement``。

    保留供外部/測試以 (db, cycle, emp) 呼叫；build_settlements 迴圈內已查得
    ``existing`` → 直接呼叫 ``_deductions_from_settlement`` 免再查同一列（main N+1）。
    """
    existing = db.scalar(
        select(YearEndSettlement).where(
            YearEndSettlement.year_end_cycle_id == cycle.id,
            YearEndSettlement.employee_id == emp.id,
        )
    )
    return _deductions_from_settlement(db, cycle, emp, existing)


def _deductions_from_settlement(
    db: Session,
    cycle: YearEndCycle,
    emp: Any,
    existing: "YearEndSettlement | None",
    auto: "AttendanceDeductions | None" = None,
) -> DeductionBreakdown:
    """用「已取得的 ``existing`` 列」組 DeductionBreakdown（B7 ⑤a wiring）。

    抽出此函式讓 build_settlements 直接用迴圈中已查得的 ``existing``，避免每位員工
    又重查一次同一列（main N+1 重構）。仍需 (db, cycle, emp) 跑 B5 自動扣款。

    分兩類扣項：

    **自動計算（4 欄，B5 ⑤a）**：meeting / personal_leave / sick_leave / late_early
    一律由 ``derive_attendance_deductions(db, cycle, emp)`` 重算（Excel 年終定額罰則：
    遲到/未打卡/事假/病假/會議缺席）。B5 回**負值**，與 engine 慣例一致
    （compute_deduction_total 直接 Σ、payable = subtotal + deduction_total 相加），
    **直接填入 DeductionBreakdown，不可再取負**。
      - **override 優先**：若既有 settlement 的 ``calc_meta`` 有對應
        ``deduction_<x>_override`` key（deduction_meeting_override /
        deduction_personal_leave_override / deduction_sick_leave_override /
        deduction_late_override）→ 採 override 值（防禦/未來；目前無 endpoint 設這些 key）。
        以 ``is not None`` 判定（非 truthiness），讓 override="0" 也被尊重。值存字串。

    **維持讀回既有值（2 欄，B5 不碰）**：
      - leave_late_prev（deduction_leave_late，去年底前合併）
      - disciplinary（deduction_disciplinary，手動獎懲，如大過 -6000）
    這是 override 回歸的關鍵：HR 手填的 disciplinary 必須跨 re-build 存活。

    對應：
      deduction_leave_late      → leave_late_prev   （讀回既有）
      deduction_disciplinary    → disciplinary      （讀回既有）
      deduction_meeting         → meeting           （B5 / override）
      deduction_personal_leave  → personal_leave    （B5 / override）
      deduction_sick_leave      → sick_leave        （B5 / override）
      deduction_late            → late_early        （B5 / override）
    """
    # B5 自動扣款（負值或 0）。build loop 預算 batch（cfg 取一次）後以 auto 傳入，避免
    # 每員工重取 cfg 的 N+1；單員工/測試路徑 auto=None 時 fallback per-employee 重算。
    if auto is None:
        from services.year_end.auto_derive.attendance_deductions import (
            derive_attendance_deductions,
        )

        auto = derive_attendance_deductions(db, cycle, emp)

    calc_meta = (existing.calc_meta or {}) if existing is not None else {}

    def _resolve(meta_key: str, auto_value: Decimal) -> Decimal:
        """override（calc_meta[meta_key]）優先；否則用 B5 計算值。

        以 is not None 判定（非 truthiness），讓 override="0" 也被尊重。
        """
        override = calc_meta.get(meta_key)
        if override is not None:
            return Decimal(str(override))
        return auto_value

    # 讀回既有（B5 不碰）：leave_late_prev / disciplinary
    if existing is None:
        leave_late_prev = Decimal("0")
        disciplinary = Decimal("0")
    else:
        leave_late_prev = Decimal(str(existing.deduction_leave_late or 0))
        disciplinary = Decimal(str(existing.deduction_disciplinary or 0))

    return DeductionBreakdown(
        leave_late_prev=leave_late_prev,
        disciplinary=disciplinary,
        meeting=_resolve("deduction_meeting_override", auto.meeting),
        personal_leave=_resolve(
            "deduction_personal_leave_override", auto.personal_leave
        ),
        sick_leave=_resolve("deduction_sick_leave_override", auto.sick_leave),
        late_early=_resolve("deduction_late_override", auto.late),
    )


# --------------------------------------------------------------------------- #
# (C) build_settlements 編排                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class BuildResult:
    built: int = 0
    skipped_finalized: int = (
        0  # skipped because already signed or finalized (non-DRAFT)
    )
    # B7：refresh_rates=True 時帶回 auto_derive 編排結果（unmatched/fallback 提醒用）；
    # refresh_rates=False（不跑 derive_all）時為 None。型別 auto_derive.DeriveReport。
    derive_report: "DeriveReport | None" = None


def _is_postgres(db: Session) -> bool:
    return (db.bind is not None) and db.bind.dialect.name == "postgresql"


def _advisory_lock_build(db: Session, academic_year: int) -> None:
    """transaction-scope advisory lock（PostgreSQL）；SQLite 測試 no-op。"""
    if not _is_postgres(db):
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"ye_build:{academic_year}"},
    )


def build_settlements(
    db: Session,
    academic_year: int,
    included_resigned_ids: set[int] | list[int] | None,
    actor_id: int | None,
    *,
    refresh_rates: bool = True,
    only_employee_ids: set[int] | None = None,
) -> BuildResult:
    """跨員工跑年終 6-step 引擎 + upsert snapshot/settlement（idempotent）。

    參與者 = ACTIVE 員工 ∪ included_resigned_ids（對齊 appraisal_sync 慣例）。
    非 DRAFT 的 settlement（已簽或已核定）不覆寫（skipped_finalized += 1）。
    本函式只 flush 不 commit；交由 router 層 transactional dep 與 audit middleware。

    only_employee_ids 不為 None 時，僅重算這些員工（其餘不動、版本不 bump）。
    供 manual_patch 單筆微調用，避免一次手調觸發整個 cycle 全員重算與版本 churn。
    """
    _advisory_lock_build(db, academic_year)
    included = set(included_resigned_ids or set())

    cycle = db.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == academic_year)
    )
    if cycle is None:
        raise ValueError(f"找不到 academic_year={academic_year} 的 YearEndCycle")

    derive_report = None
    if refresh_rates:
        refresh_enrollment_rates(db, cycle)
        # B7：refresh 階段（refresh_rates=True）才跑 auto_derive 編排（① ③ ④ ⑥）。
        # 必須在 per-employee loop 之前——loop 內 gather_performance_rates 讀 ⑥ 寫的
        # returning_student_rate、special_total sum 讀 ① ③ ④ 寫的 special_bonus_items。
        # 內部 SalaryEngine(load_from_db=True) 自開 session 讀 BonusConfig：呼叫端須確保
        # reference data 已 committed/一致（正常 HR 流程 config 早於 build 已 commit）。
        from services.year_end.auto_derive import derive_all

        # P1-2 finalized drift 護欄：查本 cycle 所有「非 DRAFT」settlement 的 employee_id
        # 集合（鏡像下方 per-employee loop 的 skip 判定 status != DRAFT）。傳給 derive_all
        # 讓 ① ③ ④ 不覆寫這些員工已凍結的 special_bonus_items（finalized settlement 在
        # loop 開頭被 skip、金額凍結，底層 items 若被改 → 凍結總額與 items 漂移、稽核對不上）。
        non_draft_ids = set(
            db.scalars(
                select(YearEndSettlement.employee_id).where(
                    YearEndSettlement.year_end_cycle_id == cycle.id,
                    YearEndSettlement.status != YearEndSettlementStatus.DRAFT,
                )
            ).all()
        )
        # P1-1：③ 參與者對齊 build（ACTIVE ∪ included_resigned_ids）；①④⑥ 不需。
        derive_report = derive_all(
            db,
            cycle,
            included_resigned_ids=included,
            skip_settlement_employee_ids=non_draft_ids,
        )

    # 參與者：ACTIVE ∪ included_resigned_ids
    employees = list(
        db.scalars(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).all()
    )
    seen_ids = {e.id for e in employees}
    if included:
        extra = db.scalars(select(Employee).where(Employee.id.in_(included))).all()
        for e in extra:
            if e.id not in seen_ids:
                employees.append(e)
                seen_ids.add(e.id)

    # only_employee_ids：單筆 manual_patch 重算用，縮到指定員工避免全 cohort 重算/版本 churn
    if only_employee_ids is not None:
        employees = [e for e in employees if e.id in only_employee_ids]

    result = BuildResult(derive_report=derive_report)

    # Fix 3: 職位薪資標準只載入一次，避免每位員工重建 SalaryEngine + 查一次 DB
    _standards = load_position_salary_standards(db)
    # 節慶基數只依角色（非員工），且查的是全域最新 BonusConfig；memoize 避免每位員工各查一次
    _festival_cache: dict["str | None", Decimal] = {}
    # 全校達成率（兩學期）只依 cycle、與員工無關；迴圈外查一次避免 gather_performance_rates
    # 每位員工各查兩列 OrgYearSettings（原 2N → 2）。
    _org_first = db.scalar(
        select(OrgYearSettings.school_achievement_rate).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first == True,  # noqa: E712
        )
    )
    _org_second = db.scalar(
        select(OrgYearSettings.school_achievement_rate).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first == False,  # noqa: E712
        )
    )
    _school_rates = (
        Decimal(str(_org_first)) if _org_first is not None else None,
        Decimal(str(_org_second)) if _org_second is not None else None,
    )

    # B5 ⑤a 考勤扣款 batch：cfg 只取一次，避免 build loop 每員工重取的 N+1。
    from services.year_end.auto_derive.attendance_deductions import (
        derive_all_attendance_deductions,
    )

    _auto_deductions = derive_all_attendance_deductions(db, cycle, employees)

    for emp in employees:
        existing = db.scalar(
            select(YearEndSettlement).where(
                YearEndSettlement.year_end_cycle_id == cycle.id,
                YearEndSettlement.employee_id == emp.id,
            )
        )
        if existing is not None and existing.status != YearEndSettlementStatus.DRAFT:
            result.skipped_finalized += 1
            continue

        base = year_end_base_salary(db, emp, standards=_standards)
        _role_key = role_key_of(emp)
        if _role_key not in _festival_cache:
            _festival_cache[_role_key] = festival_base_for_role(db, _role_key)
        festival = _festival_cache[_role_key]
        # Fix 1 (CRITICAL): 年終比例計算以民國曆年（Jan 1–Dec 31）為基準，非學年 Aug–Jul
        proration_start = date(cycle.academic_year + 1911, 1, 1)
        proration_end = date(cycle.academic_year + 1911, 12, 31)
        auto_months = compute_hire_months(emp, proration_start, proration_end)
        # Fix 2: 若已有人工覆寫值（Task 6 手動設定），優先採用；否則使用自動計算值
        _override = (
            (existing.calc_meta or {}).get("hire_months_override") if existing else None
        )
        hire_months = Decimal(str(_override)) if _override is not None else auto_months
        worked_first, worked_second = worked_semesters(emp, cycle)
        rates = gather_performance_rates(db, cycle, emp, school_rates=_school_rates)
        org_rate = resolve_org_achievement_rate(
            rates.school_rate_first,
            rates.school_rate_second,
            worked_first=worked_first,
            worked_second=worked_second,
        )
        # existing 已在迴圈頂端查得；直接組 DeductionBreakdown，免再查同一列（main N+1）。
        # B5 ⑤a 考勤扣款用迴圈外預算的 batch 結果（cfg 取一次）；缺漏則 auto=None
        # fallback per-employee 重算。
        deductions = _deductions_from_settlement(
            db, cycle, emp, existing, auto=_auto_deductions.get(emp.id)
        )

        special_raw = db.scalar(
            select(func.coalesce(func.sum(SpecialBonusItem.amount), 0)).where(
                SpecialBonusItem.year_end_cycle_id == cycle.id,
                SpecialBonusItem.employee_id == emp.id,
            )
        )
        special_total = (
            Decimal(str(special_raw)) if special_raw is not None else Decimal("0")
        )

        computed = compute_settlement(
            base_salary=base,
            festival_total=festival,
            performance_rates=rates,
            org_achievement_rate=org_rate,
            deductions=deductions,
            hire_months=hire_months,
            special_bonus_total=special_total,
        )

        is_resigned = getattr(emp, "resign_date", None) is not None or not getattr(
            emp, "is_active", True
        )
        # upsert snapshot
        snapshot = db.scalar(
            select(EmployeeYearEndSnapshot).where(
                EmployeeYearEndSnapshot.year_end_cycle_id == cycle.id,
                EmployeeYearEndSnapshot.employee_id == emp.id,
            )
        )
        if snapshot is None:
            snapshot = EmployeeYearEndSnapshot(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
            )
            db.add(snapshot)
        snapshot.base_salary = base
        snapshot.festival_total = festival
        snapshot.role = role_key_of(emp)
        snapshot.classroom_id = getattr(emp, "classroom_id", None)
        snapshot.hire_date = getattr(emp, "hire_date", None)
        snapshot.resign_date = getattr(emp, "resign_date", None)
        snapshot.hire_months = hire_months
        snapshot.is_resigned = is_resigned
        snapshot.is_contracted = True
        db.flush()  # 取得 snapshot.id

        # upsert settlement
        if existing is None:
            existing = YearEndSettlement(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                snapshot_id=snapshot.id,
            )
            db.add(existing)
        else:
            existing.snapshot_id = snapshot.id
            existing.version = (existing.version or 1) + 1

        # step 1 明細率
        existing.school_rate_first = rates.school_rate_first
        existing.school_rate_second = rates.school_rate_second
        existing.class_returning_rate_first = rates.class_returning_rate_first
        existing.class_returning_rate_second = rates.class_returning_rate_second
        existing.class_performance_rate_first = rates.class_performance_rate_first
        existing.class_performance_rate_second = rates.class_performance_rate_second
        existing.avg_performance_rate = computed.avg_performance_rate
        # step 2
        existing.base_salary = base
        existing.festival_total = festival
        existing.gross_amount = computed.gross_amount
        # step 3
        existing.org_achievement_rate = org_rate
        existing.subtotal_amount = computed.subtotal_amount
        # step 4（扣項：自動 4 欄由 B5 ⑤a 重算/override；leave_late_prev/disciplinary 讀回既有；見 gather_deductions）
        existing.deduction_leave_late = deductions.leave_late_prev
        existing.deduction_disciplinary = deductions.disciplinary
        existing.deduction_meeting = deductions.meeting
        existing.deduction_personal_leave = deductions.personal_leave
        existing.deduction_sick_leave = deductions.sick_leave
        existing.deduction_late = deductions.late_early
        existing.deduction_total = computed.deduction_total
        # step 5
        existing.hire_months = hire_months
        existing.proration_rate = computed.proration_rate
        existing.payable_amount = computed.payable_amount
        # step 6
        existing.special_bonus_total = computed.special_bonus_total
        existing.total_amount = computed.total_amount
        # 維持非 finalized 狀態（新建預設 DRAFT；既有保留現狀）
        if existing.status is None:
            existing.status = YearEndSettlementStatus.DRAFT

        db.flush()
        result.built += 1

    # TODO(task6): audit 由 router 層 audit middleware 記錄（對齊 appraisal_sync
    # void_payouts 慣例：service 不直寫 AuditLog；actor_id 由 router 帶入）。
    logger.info(
        "build_settlements: academic_year=%s built=%d skipped_finalized=%d actor=%s",
        academic_year,
        result.built,
        result.skipped_finalized,
        actor_id,
    )
    return result
