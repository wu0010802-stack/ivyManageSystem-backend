"""B3 ③ 節慶差額（FESTIVAL_DIFF）自動推導。

Excel「114上節慶獎金比例差額」：每月一個 block，逐月「應領 − 已領」加總，
多退少補（**可為負**）。上學期 6 個月（N.8 ～ N+1.1）：

    差額_m = 應領_m − 已發_m
    6 個月加總（Decimal HALF_UP，可正可負）→ FESTIVAL_DIFF special_bonus_item

---------------------------------------------------------------------------
**已發_m（payroll 實付逐月 accrual）**
---------------------------------------------------------------------------
重用 payroll 引擎 `SalaryEngine.calculate_period_accrual_row`（lumping 之前的
逐月 accrual 值）。**不**讀 `SalaryRecord.festival_bonus`——payroll 只在發放月
（2/6/9/12）把節慶獎金寫進 SalaryRecord，其餘月為 0（engine.py:1547-1556），
逐月加總會嚴重低估。calculate_period_accrual_row 內部：
  - 走 `_resolve_classroom_for_employee_in_month`（學期反查當期班級）；
  - 副班導/美師經 `assistant_to_classes_map` / `art_to_classes_map` 反查正確
    班級並對跨多班加權平均（per-class，非全校）；
  - 套 payroll 自己的目標（TARGET_ENROLLMENT 設定）+ round_half_up + 3 個月
    eligibility 規則。
ctx 建法對齊 `api/salary/festival.py`（monthly_ctx_cache：school_active /
classroom_count_map / classroom_map / meeting_absent_count_map +
assistant_to_classes_map / art_to_classes_map）。

---------------------------------------------------------------------------
**應領_m（年終 authoritative 人數，未封頂）**
---------------------------------------------------------------------------
    應領_m = base × (年終在園人數_m / 年終目標)        ← **未封頂**（比例可 > 1）
  base   = festival_base_for_role(role)（BonusConfig 角色基數查表）
  年終人數 = count_enrolled_on(db, month_end, classroom_id=該班)
           （班導/副班導/美師都用其所屬班；非帶班/辦公室用全校 count_enrolled_on）
  年終目標 = 班級 ClassEnrollmentTarget(semester_first=True).head_count_target
           （帶班）/ OrgYearSettings(semester_first=True).enrollment_target（全校）

**為何不用 `calculate_festival_bonus_v2` 算應領**：v2 的目標來自 payroll
config（TARGET_ENROLLMENT），而本欄的語意正是「以 *年終* 目標重算應領、與
payroll *config* 目標的差」。Excel 蔡宜倩 12 月為例：
  應領 2166.67 = 2000 × 13 / **12**（年終 ClassEnrollmentTarget=12）
  已領 1857    = 2000 × 13 / **14**（payroll config target=14）
12-vs-14 的目標差正是這個「人數校正 true-up」。故應領直接用 raw
`base × 人數 / 年終目標`，未封頂（蔡 14/12 = 1.1667 → 2333 > base，獎勵超收）。

---------------------------------------------------------------------------
**per-class vs 全校的分流：以 payroll category 為唯一判定（修舊版 P0-1）**
---------------------------------------------------------------------------
舊版讓副班導/美師落入 else 用「全校比例」算應領，但 payroll 用「當班比例」算已發
→ 系統性對不上。本版**逐月以 payroll 回傳的 `category` 決定應領分流**，兩側必一致：
  - category == "帶班老師" → per-class：應領用該員工當學期所屬班的 count_enrolled_on
    + 該班 ClassEnrollmentTarget.head_count_target。班級反查 `_resolve_classroom_for_emp`
    **逐字鏡像** payroll engine（含 cross-term fallback：無當期班時退跨學期任一 active）。
  - category in ("主管","辦公室","其他") → 全校：應領用全校 count_enrolled_on +
    OrgYearSettings.enrollment_target（與 payroll 全校比例同基準）。
  已發：calculate_period_accrual_row 內部以 assistant_to_classes_map /
  art_to_classes_map 反查正確班級並對跨多班加權（payroll 既有邏輯）。
**為何 gate on category 而非「是否在 roster」**：主任/組長同時掛班導 roster 時，payroll
優先序 主管 > 辦公室 > 帶班 → category "主管" 走全校；若應領僅看 roster 會誤判 per-class
→ 兩側基數/比例皆不同 → 差額 garbage。以 category 判定根除此分歧。

**eligibility 對稱（修新人 windfall P1）**：應領與已發套用**完全相同**的 festival
eligibility 判定——未滿 `festival_bonus_months`（_attendance_policy，預設 3）個月的新人，
該月應領 = 0（與 payroll calculate_period_accrual_row 對該月的 gate 一致）。reference_date
取該月月底（鏡像 payroll `_get_bonus_reference_date(y, m)`「發放月當月才滿三個月」語意）。
故新人早月（未滿資格）應領 0、已發亦 0 → diff = 0，**不**產生憑空的正向 true-up windfall。
Excel `114上節慶獎金比例差額` 佐證：年中加入者（如楊思瑜 8 月）該月整欄空白/0，不照給應領。

**已知限制（NEEDS_CONTEXT，見 task report）**：
  1. base=0 角色（廚房/護理/美語等）刻意排除（festival_base <= 0 → skip）：應領恆
     0，若 payroll 仍發 festival，全負差額會變成「回收」而非「true-up」，非本欄職責。
  2. 多班副班導/美師：payroll（已發）對跨多班加權平均，應領以單班 count_enrolled_on
     計（取主要班）。Excel 僅佐證單班情境（呂宜凡/天堂鳥）。多班加權差異待 phase1.5。

override 慣例見 auto_derive/__init__.py：source_ref 以 ``auto:`` 標記自動筆；
手動筆（source_ref 非 auto: 開頭）絕不覆寫（對齊 B2 _upsert_auto_item）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from models.classroom import Classroom
from models.employee import Employee
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from services.salary.engine import SalaryEngine
from services.year_end.enrollment_rates import count_enrolled_on
from services.year_end.settlement_builder import (
    _semester_month_ends,
    _semester_ranges,
    festival_base_for_role,
    role_key_of,
)
from utils.academic import resolve_current_academic_term
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


def _pick_primary(rows: list, emp_id: int) -> Optional[Classroom]:
    """從候選班級挑主要班（head > assistant > art），對齊 engine._pick_primary_classroom。"""
    head = next((c for c in rows if c.head_teacher_id == emp_id), None)
    if head is not None:
        return head
    assistant = next((c for c in rows if c.assistant_teacher_id == emp_id), None)
    if assistant is not None:
        return assistant
    return next((c for c in rows if c.art_teacher_id == emp_id), None)


def _resolve_classroom_for_emp(
    db: Session, emp_id: int, school_year: int, semester: int
) -> Optional[Classroom]:
    """員工當學期所屬主要班級（head > assistant > art），無→None。

    **逐字對齊** payroll `engine._resolve_classroom_for_employee_in_term`（含 cross-term
    fallback）：先以 (school_year, semester) 篩當期班級；若無，fallback 至跨學期任一
    active 班（id DESC 取最新）。fallback 用於「學校未替每學期建立獨立班級紀錄、單班
    沿用」的相容情境——payroll 的「已發」此時也走 fallback 解析到班，若本「應領」端
    不跟進就會 per-class（已發）vs 全校（應領）對不上（同舊版 P0 的 legacy-data 變體）。
    以 Classroom 的 head/assistant/art_teacher_id 為準（非冗餘 Employee.classroom_id）。
    """
    base_filter = or_(
        Classroom.head_teacher_id == emp_id,
        Classroom.assistant_teacher_id == emp_id,
        Classroom.art_teacher_id == emp_id,
    )
    term_rows = list(
        db.scalars(
            select(Classroom)
            .where(
                Classroom.is_active.is_(True),
                Classroom.school_year == school_year,
                Classroom.semester == semester,
                base_filter,
            )
            .order_by(Classroom.id.asc())
        ).all()
    )
    if term_rows:
        return _pick_primary(term_rows, emp_id)

    # cross-term fallback（鏡像 payroll）：無當期班 → 取跨學期任一 active（id DESC）。
    any_rows = list(
        db.scalars(
            select(Classroom)
            .where(Classroom.is_active.is_(True), base_filter)
            .order_by(Classroom.id.desc())
        ).all()
    )
    picked = _pick_primary(any_rows, emp_id)
    if picked is not None:
        logger.warning(
            "員工 %s 在 school_year=%s semester=%s 無對應 active 班級；"
            "fallback 使用 classroom_id=%s（鏡像 payroll；學校未建該學期班級紀錄？）",
            emp_id,
            school_year,
            semester,
            picked.id,
        )
    return picked


def _class_target(db: Session, cycle_id: int, classroom_id: int) -> Optional[int]:
    """該班上學期 ClassEnrollmentTarget.head_count_target（無→None）。"""
    row = db.scalar(
        select(ClassEnrollmentTarget).where(
            ClassEnrollmentTarget.year_end_cycle_id == cycle_id,
            ClassEnrollmentTarget.semester_first.is_(True),
            ClassEnrollmentTarget.classroom_id == classroom_id,
        )
    )
    return int(row.head_count_target) if row is not None else None


def _build_meeting_absent_cache(
    db: Session, month_ends: list[date]
) -> dict[tuple[int, int], dict[int, int]]:
    """逐月會議缺席數預載（employee_id → count），避免 calculate_period_accrual_row
    在迴圈內每員工一次 MeetingRecord.count()（對齊 api/salary/festival.py:183-198）。

    **刻意只預載 meeting_absent_count_map**：此查詢與 payroll 自身用的 MeetingRecord
    查詢逐字一致，注入後「已發」仍忠實。
    **不**預載 school_active / classroom_count_map——payroll 的「已發」必須走 engine
    自己的 `count_students_active_on`（services/student_enrollment.py：僅 enrollment/
    graduation filter，**不含 withdrawal**），而本模組的「應領」走 `count_enrolled_on`
    （含 withdrawal_date > d）。兩者人數定義刻意不同：差額正是這個「人數校正 true-up」
    （task 語意：已領用 payroll 自己的人數，可能與年終人數不同）。若把 count_enrolled_on
    注入 ctx，會把已發強行套到年終人數函式上，消掉 true-up 的人數校正成分。
    """
    from sqlalchemy import func

    from models.database import MeetingRecord

    cache: dict[tuple[int, int], dict[int, int]] = {}
    for month_end in month_ends:
        y, m = month_end.year, month_end.month
        month_start = date(y, m, 1)
        rows = (
            db.query(MeetingRecord.employee_id, func.count(MeetingRecord.id))
            .filter(
                MeetingRecord.meeting_date >= month_start,
                MeetingRecord.meeting_date <= month_end,
                MeetingRecord.attended == False,  # noqa: E712
            )
            .group_by(MeetingRecord.employee_id)
            .all()
        )
        cache[(y, m)] = {int(eid): int(cnt or 0) for eid, cnt in rows}
    return cache


def derive_festival_diff(db: Session, cycle: YearEndCycle) -> FestivalDiffReport:
    """推導 ③ 節慶差額 → upsert special_bonus_items（FESTIVAL_DIFF）。

    只 flush（由呼叫端 commit）。idempotent；手動筆不覆寫（見 __init__ override 慣例）。
    參與者 = 在職且 festival 基數 > 0 的員工。

    已發 = SalaryEngine.calculate_period_accrual_row（payroll 逐月 accrual，
           副班導/美師 per-class + 封頂 + 學期反查）。
    應領 = base × 年終人數 / 年終目標（未封頂，per-class/全校依角色）。
    """
    report = FestivalDiffReport()
    label = period_label(cycle)
    month_ends = _semester_month_ends(cycle, semester_first=True)

    # 上學期對應的學年/學期（用於班級反查；payroll engine 內部用月份各自反查，
    # 此處的 (sy, sem) 僅供「應領」端的 _resolve_classroom_for_emp 與 ClassEnrollmentTarget
    # 對齊。上學期月份解析出的學期應一致；以第一個月底反查最穩。）
    (f_start, _), _ = _semester_ranges(cycle)
    sy, sem = resolve_current_academic_term(f_start)

    # 全校目標（上學期 OrgYearSettings）— 非帶班員工用
    org = db.scalar(
        select(OrgYearSettings).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first.is_(True),
        )
    )
    school_target = org.enrollment_target if org is not None else None

    # payroll engine：從 DB 載 config（與正式月薪同一套目標/基數/grade map）。
    engine = SalaryEngine(load_from_db=True)

    # 跨月共用：active 班級 + 副班導/美師 → 班級清單反查表（對齊 api/salary/festival.py）。
    # 這兩張 map 餵給 calculate_period_accrual_row，讓「已發」對副班導/美師跨多班做
    # per-class 加權（payroll 既有邏輯）；非全校。預載 grade 避免 N+1。
    all_active_classrooms = list(
        db.scalars(
            select(Classroom)
            .options(joinedload(Classroom.grade))
            .where(Classroom.is_active.is_(True))
        ).all()
    )
    assistant_to_classes_map: dict[int, list] = {}
    art_to_classes_map: dict[int, list] = {}
    for c in all_active_classrooms:
        if c.assistant_teacher_id:
            assistant_to_classes_map.setdefault(c.assistant_teacher_id, []).append(c)
        if c.art_teacher_id:
            art_to_classes_map.setdefault(c.art_teacher_id, []).append(c)

    # 只預載會議缺席數（與 payroll 同一查詢，忠實）；人數交給 engine 自己的
    # count_students_active_on 算（見 _build_meeting_absent_cache docstring）。
    meeting_absent_cache = _build_meeting_absent_cache(db, month_ends)

    employees = list(
        db.scalars(select(Employee).where(Employee.is_active.is_(True))).all()
    )

    # payroll category（"帶班老師"）→ 應領 per-class；("主管"/"辦公室"/"其他") → 全校。
    _CLASS_CATEGORY = "帶班老師"

    for emp in employees:
        festival_base = festival_base_for_role(db, role_key_of(emp))
        # festival 基數 = 0 的角色（廚房/護理/美語/無法分類）刻意排除，避免「全負回收」。
        if festival_base <= 0:
            continue

        # 候選班級（per-class 用）：鏡像 payroll 解析（含 cross-term fallback）。
        # 實際 per-class vs 全校由「payroll 當月 category」決定（見下），避免「主任/組長
        # 同時掛班導」時 payroll 走主管全校、應領誤走 per-class 的不一致。
        cand_classroom = _resolve_classroom_for_emp(db, emp.id, sy, sem)
        cand_target = (
            _class_target(db, cycle.id, cand_classroom.id)
            if cand_classroom is not None
            else None
        )

        total_diff = Decimal("0")
        months_meta: list[dict] = []
        skip_emp = False
        for month_end in month_ends:
            y, m = month_end.year, month_end.month

            # ── 已發_m：payroll 逐月 accrual（per-class + 封頂 + 學期反查）──
            # 刻意不塞 school_active_students / classroom_count_map：讓 engine 走自己的
            # count_students_active_on（payroll 真實人數，不含 withdrawal），與「應領」的
            # count_enrolled_on 人數定義刻意不同 → 差額含「人數校正 true-up」。
            # 不帶 "classroom" key → engine 走 _resolve_classroom_for_employee_in_month。
            per_month_ctx = {
                "session": db,
                "employee": emp,
                "meeting_absent_count_map": meeting_absent_cache[(y, m)],
                "assistant_to_classes_map": assistant_to_classes_map,
                "art_to_classes_map": art_to_classes_map,
            }
            accrual = engine.calculate_period_accrual_row(
                emp.id, y, m, _ctx=per_month_ctx
            )
            paid = _q2(accrual.get("festival_bonus") or 0)
            category = accrual.get("category", "")

            # ── eligibility 對稱 gate（修新人 windfall P1）──
            # payroll「已發」對未滿 festival_bonus_months（預設 3）個月的新人 gate
            # festival=0（engine.calculate_festival_bonus_breakdown:1994-1998）。
            # 「應領」必須套**完全相同**的判定：reference_date = 該月月底 month_end
            # （= payroll `_get_bonus_reference_date(y, m)` 回傳值，逐字鏡像「在發放月
            # 當月才滿三個月」語意，engine.py:1029-1037），festival_bonus_months 同樣
            # 由 engine 從 _attendance_policy 讀（不硬寫 3，避免與 payroll config desync）。
            # 不 eligible → 應領 = 0 → diff = 0（paid 同 gate 亦 0），不對「資格未到」
            # 的月份 true-up（憑空發給未滿資格者 = windfall）。**先 gate 再算 target**：
            # 避免「資格未到月份」誤觸下方 target 缺漏的 skip_emp/break（那會連同該員工
            # 後段已滿資格月份的正常 true-up 一併丟棄）；也省一次 count_enrolled_on。
            if not engine.is_eligible_for_festival_bonus(
                emp.hire_date, reference_date=month_end
            ):
                # paid 與 due 走同一 gate；保留 diff = due - paid 而非硬寫 0：
                # 若 paid 異常非 0（payroll 不一致）會反映在 diff，不被遮蔽。
                due = Decimal("0")
                diff = due - paid
                total_diff += diff
                months_meta.append(
                    {
                        "month": f"{y}-{m:02d}",
                        "category": category,
                        "classroom_id": None,
                        "enrolled": None,
                        "target": None,
                        "eligible": False,
                        "due": str(due),
                        "paid": str(paid),
                        "diff": str(diff),
                    }
                )
                continue

            # ── 應領_m：依 payroll category 決定 per-class vs 全校（兩側必一致）──
            if category == _CLASS_CATEGORY and cand_classroom is not None:
                classroom_id: Optional[int] = cand_classroom.id
                target = cand_target
                if target is None:
                    # 帶班但無該班年終目標 → 退全校（保守，記 warning 一次）。
                    if not any(
                        w.startswith(f"員工 {emp.id} 帶班") for w in report.warnings
                    ):
                        report.warnings.append(
                            f"員工 {emp.id} 帶班(classroom_id={cand_classroom.id})但無 "
                            f"ClassEnrollmentTarget，退全校目標"
                        )
                    classroom_id = None
                    target = school_target
            else:
                # 主管/辦公室/其他 → 全校（與 payroll 全校比例同基準）。
                classroom_id = None
                target = school_target

            if target is None or int(target) <= 0:
                report.warnings.append(
                    f"員工 {emp.id} ({y}-{m:02d}) 目標人數 <= 0 或缺，略過該員工"
                )
                skip_emp = True
                break

            target_d = Decimal(str(target))
            enrolled = count_enrolled_on(db, month_end, classroom_id=classroom_id)
            due = _q2(festival_base * Decimal(enrolled) / target_d)

            diff = due - paid
            total_diff += diff
            months_meta.append(
                {
                    "month": f"{y}-{m:02d}",
                    "category": category,
                    "classroom_id": classroom_id,
                    "enrolled": enrolled,
                    "target": int(target),
                    "eligible": True,
                    "due": str(due),
                    "paid": str(paid),
                    "diff": str(diff),
                }
            )

        if skip_emp:
            continue

        # 員工層 classroom_id：只看「eligible 月份」的歸屬——gated 月份的
        # classroom_id 是非語意 placeholder（None），不應稀釋 attribution，否則
        # 年中入職的班導會因早月 gated 而被誤判成無班級（is_head_teacher=False）。
        # 若所有 eligible 月份皆 per-class 同一班則記該班，否則（混合/全 gated）None。
        eligible_classroom_ids = {
            mm["classroom_id"] for mm in months_meta if mm.get("eligible")
        }
        item_classroom_id = (
            next(iter(eligible_classroom_ids))
            if len(eligible_classroom_ids) == 1
            else None
        )

        amount = _q2(total_diff)
        wrote = _upsert_auto_item(
            db,
            cycle_id=cycle.id,
            employee_id=emp.id,
            label=label,
            amount=amount,
            classroom_id=item_classroom_id,
            calc_meta={
                "festival_base": str(_q2(festival_base)),
                "is_head_teacher": item_classroom_id is not None,
                "months": months_meta,
            },
        )
        if wrote:
            report.written += 1
        else:
            report.skipped_manual += 1

    db.flush()
    logger.info(
        "festival_diff derive: cycle=%s written=%d skipped_manual=%d warnings=%d",
        cycle.academic_year,
        report.written,
        report.skipped_manual,
        len(report.warnings),
    )
    return report
