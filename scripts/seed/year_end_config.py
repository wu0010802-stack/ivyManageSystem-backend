"""scripts/seed/year_end_config.py — 年終/考核「年度設定 + 班級編制目標」設定表補齊。

目的（兩個）：
  (a) 讓「年度設定 / 班級編制目標」這些設定頁有資料可看。
  (b) **拆地雷**：dev DB 既有 19 筆 year_end_settlements 已填金額，但
      org_year_settings / class_enrollment_targets 為空。使用者一旦在年終頁點
      「build / 重算」（api/year_end build_settlements_endpoint，固定 refresh_rates=True）
      會走 refresh_enrollment_rates + 6-step 引擎；此時：
        - _school_rates 從 OrgYearSettings.school_achievement_rate 取 → 空表回 None
        - compute_avg_performance_rate(None, None, 無班級率) → Decimal("0.0")
        - org_rate = resolve_org_achievement_rate(None, None) → Decimal("0.0")
        - gross = (base+festival) × 0% = 0 → 整張 settlement 金額歸 0
      只要補上設定表，refresh 會把 school_achievement_rate 重算成
      count_enrolled_on(基準日)/enrollment_target × 100（dev DB 在 113 cycle 基準日
      在籍 16 人，enrollment_target=18 → 88.89%，非 0），地雷即拆除。

範圍（3 設定 model，全部沿用既有 cycle，不新增 cycle）：
  1. org_year_settings        每 cycle 兩列（semester_first True/False）
  2. class_enrollment_targets  每 cycle × 每班 × 兩學期
  3. grade_intake_targets      招生名額規劃面板的計畫名額（每年級 × 學年 × 學期）

【為何 recompute 後金額不再歸 0 — 真正的咽喉是 enrollment_target】
  唯一 load-bearing 的設定值是 **OrgYearSettings.enrollment_target（>0）**：
    refresh_enrollment_rates 把 school_achievement_rate 重算為
      count_enrolled_on(基準日) / enrollment_target × 100
    dev DB 在 113 cycle 基準日（2025-02-01 與各月底）在籍 16 人（學生 enrollment_date
    為 NULL/跨期，通過 _enrolled_on_filter），16/18 × 100 = 88.89% → 非 0。
  build loop 對每位員工：
    avg% = 全校達成率平均 ≈ 88.9（class_* 對本 dev DB 全 None，見下方註）。
    **org%（step3 乘數）= resolve_org_achievement_rate(school_rate_first, school_rate_second)
      ≈ 88.9 — 由「全校率」推導，build loop 並未讀 OrgYearSettings.org_achievement_rate 欄。**
    gross = (base+festival) × 88.9% > 0、subtotal = gross × 88.9% > 0 → total 非 0。
  若 enrollment_target=0 或 OrgYearSettings 兩列缺席 → school_rate 算成 0 →
  avg=0 → gross=0 → 整張 settlement 歸 0（地雷）。本 seed 提供 enrollment_target=18 即拆除。

  refresh **覆寫**：school_achievement_rate / enrollment_actual / class_performance_rate /
    avg_monthly_enrollment（由在籍資料重算）。
  refresh **不覆寫（display-only，build loop 也不直接消費）**：org_achievement_rate /
    returning_student_rate / enrollment_target / head_count_target /
    head_teacher_employee_id。其中只有 enrollment_target 經 refresh 間接影響金額（當分母），
    其餘為設定頁顯示用；org_achievement_rate（83.6）與預埋的 school_rate（87.5/91.5）純供
    設定頁初始顯示，非金額來源。

  【班級率對本 dev DB 不參與平均】role_key_of 以 employee.position（此 dev DB 為
    「幼兒園教師」「教保員」，非「班導」「副班導」）判定帶班角色 → _has_class_role 全 False，
    故 gather_performance_rates 對所有員工 class_* 皆 None；class_enrollment_targets 純為
    設定頁顯示資料，當下不餵任何員工的 avg。拆雷完全靠全校率（OrgYearSettings）。

  實測 19 名在職員工跑真正的 build_settlements(refresh_rates=True)：7 筆 DRAFT 重算後皆非 0
  （~10.5k–12.9k），12 筆已簽/已核定 skip 不動；0 名歸 0，全在 ±100 萬 CHECK 內（驗證已 rollback）。

【冪等契約】每筆先以唯一鍵 exists 查；存在即跳過（0 修改、0 刪除）。重跑新增 0 筆。
  本 seed 只補設定表，**不**碰 year_end_settlements（那是 appraisal_yearend.py 範圍）。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select

from models.classroom import Classroom, Student
from models.recruitment import GradeIntakeTarget
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    YearEndCycle,
)

from scripts.seed._common import (
    get_classrooms,
    session_scope,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 示範常數（對齊 appraisal_yearend.py 既有 settlement 的 demo 值，保持一致）   #
# --------------------------------------------------------------------------- #

# 全校招生目標：dev DB 在 113 cycle 基準日在籍 16 人；target=18 → 88.89%（非 0、合理）。
# 取 18 而非 CLAUDE.md 提到的全校 160/176，是因為 dev DB 只灌了 16 名在籍學生，
# 用 160 會讓 refresh 算出 10% 全校率、年終獎金縮水 ~9 倍且失真；18 落在既有 demo
# 87.5/91.5 的合理區間，且 refresh 後仍非 0。
_ENROLLMENT_TARGET = 18
# 基準日當下實際在籍（settings 頁初始顯示用；refresh 會以實際在籍覆寫，值一致）。
_ENROLLMENT_ACTUAL = 16
# 全校達成率上/下（settings 頁初始顯示用；refresh 以 16/18×100=88.89 覆寫，方向一致）。
_SCHOOL_RATE_FIRST = Decimal("87.5")
_SCHOOL_RATE_SECOND = Decimal("91.5")
# 機構達成比率（Excel「年終獎金」sheet 的達成比率）。**純設定頁顯示用**：
# build loop 的 step3 org% 由 resolve_org_achievement_rate(全校率) 推導，並不讀此欄。
_ORG_ACHIEVEMENT_RATE = Decimal("83.6")
# 自強活動 / 機構會議單次未到扣款（model 預設亦 1000）。
_MEETING_ABSENCE_DEDUCTION = Decimal("1000")
# 班級舊生註冊率（小數）。**refresh 不覆寫此欄**（Phase 1 人工維護）→ 給非 0 demo 值。
_RETURNING_STUDENT_RATE = Decimal("0.926")
# 班級編制人數 buffer：head_count_target = max(實際在籍, 1) + buffer，讓 refresh 後
# class_performance_rate 落在合理 ~85% 區間（dev DB 各班在籍 8–25 人）。
_HEAD_COUNT_BUFFER = 2

# 招生名額規劃：grade_intake_targets 為招生側（名額規劃面板）；dev DB 學生在 114 學年。
_INTAKE_SCHOOL_YEAR = 114
_INTAKE_SEMESTER = 1
# 各年級計畫名額 buffer（target_seats = 該年級現有在籍 + buffer）。
_INTAKE_SEAT_BUFFER = 5


def _seed_org_year_settings(session, cycle: YearEndCycle) -> int:
    """org_year_settings：每 cycle 兩列（semester_first True/False）。

    唯一鍵 (year_end_cycle_id, semester_first)。存在即跳過。
    """
    inserted = 0
    for semester_first in (True, False):
        existing = session.scalar(
            select(OrgYearSettings).where(
                OrgYearSettings.year_end_cycle_id == cycle.id,
                OrgYearSettings.semester_first == semester_first,
            )
        )
        if existing is not None:
            continue
        session.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=semester_first,
                # enrollment_target 是唯一 load-bearing 欄：refresh 用它當分母重算
                # school_achievement_rate；>0 才能讓金額非 0（見模組 docstring）。
                enrollment_target=_ENROLLMENT_TARGET,
                enrollment_actual=_ENROLLMENT_ACTUAL,
                # 上/下學期初始顯示值；refresh 會以實際在籍覆寫（方向一致、仍非 0）。
                school_achievement_rate=(
                    _SCHOOL_RATE_FIRST if semester_first else _SCHOOL_RATE_SECOND
                ),
                # 純設定頁顯示用：build loop 的 step3 org% 由全校率推導，不讀此欄。
                org_achievement_rate=_ORG_ACHIEVEMENT_RATE,
                meeting_absence_deduction=_MEETING_ABSENCE_DEDUCTION,
                festival_bonus_meta={},
            )
        )
        inserted += 1
    return inserted


def _seed_class_enrollment_targets(session, cycle: YearEndCycle) -> int:
    """class_enrollment_targets：每 cycle × 每班 × 兩學期。

    唯一鍵 (year_end_cycle_id, semester_first, classroom_id)。存在即跳過。
    head_teacher_employee_id / assistant_employee_id 取自 classroom 現態，
    讓 gather_performance_rates 能用 head_teacher_employee_id == emp.id 對到班級率。

    注意：本 dev DB 員工 position 為「幼兒園教師/教保員」（非「班導/副班導」）→
    role_key_of 判定 _has_class_role 全 False → 班級率當下不餵任何員工的 avg，
    本表純為設定頁顯示資料。拆雷靠全校率（OrgYearSettings），不依賴本表。
    """
    inserted = 0
    for classroom in get_classrooms(session):
        active_count = (
            session.query(Student)
            .filter(
                Student.classroom_id == classroom.id,
                Student.is_active == True,  # noqa: E712
            )
            .count()
        )
        head_count_target = max(active_count, 1) + _HEAD_COUNT_BUFFER
        for semester_first in (True, False):
            existing = session.scalar(
                select(ClassEnrollmentTarget).where(
                    ClassEnrollmentTarget.year_end_cycle_id == cycle.id,
                    ClassEnrollmentTarget.semester_first == semester_first,
                    ClassEnrollmentTarget.classroom_id == classroom.id,
                )
            )
            if existing is not None:
                continue
            session.add(
                ClassEnrollmentTarget(
                    year_end_cycle_id=cycle.id,
                    semester_first=semester_first,
                    classroom_id=classroom.id,
                    head_teacher_employee_id=getattr(
                        classroom, "head_teacher_id", None
                    ),
                    assistant_employee_id=getattr(
                        classroom, "assistant_teacher_id", None
                    ),
                    head_count_target=head_count_target,
                    # 初始顯示值；refresh 以各月底在班人數平均覆寫（方向一致）。
                    avg_monthly_enrollment=Decimal(str(active_count)),
                    class_performance_rate=Decimal("0"),
                    # refresh 不覆寫 → 給非 0 demo 值（班舊生率）。
                    returning_student_rate=_RETURNING_STUDENT_RATE,
                )
            )
            inserted += 1
    return inserted


def _seed_grade_intake_targets(session) -> int:
    """grade_intake_targets：招生名額規劃面板的計畫名額（每年級 × 學年 × 學期）。

    唯一鍵 (grade_id, school_year, semester)。存在即跳過。
    target_seats = 該年級現有在籍 + buffer（招生側獨立表，不參與年終 build）。
    """
    inserted = 0
    grade_ids = [
        row[0]
        for row in session.execute(
            select(Classroom.grade_id).distinct().where(Classroom.grade_id.isnot(None))
        ).all()
    ]
    for grade_id in sorted(grade_ids):
        # 該年級現有在籍（透過 classroom.grade_id 關聯）。
        enrolled = (
            session.query(Student)
            .join(Classroom, Student.classroom_id == Classroom.id)
            .filter(
                Classroom.grade_id == grade_id,
                Student.is_active == True,  # noqa: E712
            )
            .count()
        )
        existing = session.scalar(
            select(GradeIntakeTarget).where(
                GradeIntakeTarget.grade_id == grade_id,
                GradeIntakeTarget.school_year == _INTAKE_SCHOOL_YEAR,
                GradeIntakeTarget.semester == _INTAKE_SEMESTER,
            )
        )
        if existing is not None:
            continue
        session.add(
            GradeIntakeTarget(
                grade_id=grade_id,
                school_year=_INTAKE_SCHOOL_YEAR,
                semester=_INTAKE_SEMESTER,
                target_seats=enrolled + _INTAKE_SEAT_BUFFER,
            )
        )
        inserted += 1
    return inserted


def step() -> None:
    """補齊年終/考核設定表（冪等）。只補設定，不碰 settlements。"""
    with session_scope() as session:
        cycle = (
            session.execute(select(YearEndCycle).order_by(YearEndCycle.id).limit(1))
            .scalars()
            .first()
        )
        if cycle is None:
            logger.warning("無 year_end_cycle，跳過 year_end_config seed")
            print("[seed.year_end_config] 無 year_end_cycle，跳過")
            return

        counts = {
            "org_year_settings": _seed_org_year_settings(session, cycle),
            "class_enrollment_targets": _seed_class_enrollment_targets(session, cycle),
            "grade_intake_targets": _seed_grade_intake_targets(session),
        }

    logger.info("year_end_config seed 完成：%s", counts)
    print(f"[seed.year_end_config] {counts}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
