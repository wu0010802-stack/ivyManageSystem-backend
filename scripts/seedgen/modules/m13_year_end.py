"""m13_year_end:跑真實年終引擎(build_settlements)產生 114 學年年終結算。

職責(對齊計畫 Task 3.11):
  1. 建 114 `year_end_cycles`(status OPEN)。
  2. 建 `org_year_settings`(兩學期 semester_first True/False)。
  3. 建 `class_enrollment_targets`(每班 × 兩學期,綁班導/助教與招生目標)。
  4. `ctx.session.commit()`(build_settlements 內 SalaryEngine(load_from_db=True)
     會自開 session 讀 BonusConfig,故前置 config / cycle / org / class 必須已 commit)。
  5. 呼叫 `build_settlements(session, academic_year=114, included_resigned_ids=None,
     actor_id=<admin user id>, refresh_rates=True)`,產出
     `employee_year_end_snapshots` + `year_end_settlements`(+ derive_all 寫的
     special_bonus_items)。
  6. 把產生的 settlement status 由 DRAFT 設為 SUPERVISOR_SIGNED(鏡像
     api/year_end sign_supervisor:設 status / supervisor_signed_by / supervisor_signed_at)。

與既有 caller 對齊(api/year_end build_settlements_endpoint:826):
  - build_settlements 第二參數傳 `cycle.academic_year`(**民國學年 114**,非西元 2025)。
    函式內部自行 `+1911`(proration / config_year / 學期區間皆以民國曆年推算)。
  - refresh_rates=True → 先 refresh_enrollment_rates(由在籍資料回填 stored rates)
    再跑 derive_all(① ③ ④ ⑥ 自動推導 special_bonus_items / 班級率)。

金額守衛:year_end_settlements / special_bonus_items 各有 ±100 萬 DB CHECK
(disciplinary / amount 對稱界);本模組不手填這些扣項,金額由引擎依底薪/節慶/
達成率算出,正常規模(底薪 ~3 萬、節慶 ~數千)遠在界內。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from ..context import SeedContext

logger = logging.getLogger(__name__)


def seed(ctx: SeedContext) -> None:
    """跑年終引擎產生 114 學年結算,並將結算單推進至 SUPERVISOR_SIGNED。"""
    session = ctx.session
    if session is None:  # pragma: no cover - orchestrator 必注入 session
        raise RuntimeError("m13_year_end 需要 DB session")

    from models.year_end import (
        ClassEnrollmentTarget,
        OrgYearSettings,
        YearEndCycle,
        YearEndCycleStatus,
        YearEndSettlement,
        YearEndSettlementStatus,
    )
    from services.year_end.settlement_builder import build_settlements

    academic_year = ctx.config.academic_year  # 民國學年,預設 114

    # --- 0) 解析 actor(admin user id):build_settlements actor_id 與簽核人 ---
    admin_user = ctx.users.get("admin")
    actor_id = getattr(admin_user, "id", None) if admin_user is not None else None

    # --- 1) 建 year_end_cycle(OPEN);若已存在(重跑)沿用,不重建 ---
    cycle = (
        session.query(YearEndCycle)
        .filter(YearEndCycle.academic_year == academic_year)
        .first()
    )
    if cycle is None:
        # 學年 N(民國)= 西元 N+1911 年 8 月 ~ N+1912 年 7 月。
        start_year = academic_year + 1911
        cycle = YearEndCycle(
            academic_year=academic_year,
            start_date=date(start_year, 8, 1),
            end_date=date(start_year + 1, 7, 31),
            # 結算基準日:民國曆年初(對齊 Excel「114.01.15」年終結算基準);
            # enrollment_rates.count_enrolled_on 以此日為註冊人數基準。
            bonus_calc_date=date(start_year + 1, 1, 15),
            status=YearEndCycleStatus.OPEN,
            params_snapshot={},
            created_by=actor_id,
        )
        session.add(cycle)
        session.flush()  # 取得 cycle.id 才能建子 rows
        ctx.log("year_end_cycles", 1)

    # --- 2) 建 org_year_settings(兩學期:semester_first True/False) ---
    # 招生目標對齊 dev 慣用全校編制(scale_profile.students 取整為目標);
    # org_achievement_rate / school_achievement_rate 由 refresh_enrollment_rates 重算,
    # 此處給合理初值(achievement 100 不影響——refresh 會覆寫)。
    enrollment_target = int(ctx.config.scale_profile["students"])
    existing_org_sems = {
        row.semester_first
        for row in session.query(OrgYearSettings.semester_first)
        .filter(OrgYearSettings.year_end_cycle_id == cycle.id)
        .all()
    }
    org_built = 0
    for semester_first in (True, False):
        if semester_first in existing_org_sems:
            continue
        session.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=semester_first,
                enrollment_target=enrollment_target,
                enrollment_actual=None,  # refresh_enrollment_rates 回填
                school_achievement_rate=Decimal("0"),  # refresh 重算
                school_achievement_rate_override=None,
                # 機構達成比率(step3 用);Excel 慣用 ~0.9 左右,給 1.0 即「全額」不打折,
                # 由業主後續手動調整。值落 Numeric(6,3) 界內。
                org_achievement_rate=Decimal("1.000"),
                meeting_absence_deduction=Decimal("1000"),
                festival_bonus_meta={},
            )
        )
        org_built += 1
    if org_built:
        ctx.log("org_year_settings", org_built)

    # --- 3) 建 class_enrollment_targets(每班 × 兩學期) ---
    # head_teacher_employee_id 綁 classroom.head_teacher_id(m01 回填),
    # 使 gather_performance_rates 能以 head_teacher_employee_id == emp.id 對到班級率。
    existing_ct_keys = {
        (row.classroom_id, row.semester_first)
        for row in session.query(
            ClassEnrollmentTarget.classroom_id,
            ClassEnrollmentTarget.semester_first,
        )
        .filter(ClassEnrollmentTarget.year_end_cycle_id == cycle.id)
        .all()
    }
    ct_built = 0
    for classroom in ctx.classrooms:
        head_count_target = int(getattr(classroom, "capacity", None) or 30)
        for semester_first in (True, False):
            if (classroom.id, semester_first) in existing_ct_keys:
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
                    # 舊生註冊率(小數,Phase1 人工維護,refresh 不動):給合理值 0.9。
                    returning_student_rate=Decimal("0.900"),
                    avg_monthly_enrollment=Decimal("0"),  # refresh 重算
                    class_performance_rate=Decimal("0"),  # refresh 重算
                )
            )
            ct_built += 1
    if ct_built:
        ctx.log("class_enrollment_targets", ct_built)

    # --- 4) commit:build_settlements 內 SalaryEngine(load_from_db=True) 自開 session
    #         讀 BonusConfig / 職位標準,必須先看到已 commit 的 config + cycle + org + class。
    session.commit()

    # --- 5) 跑真年終引擎(production 同路徑) ---
    #   academic_year 傳「民國學年 114」(對齊 api/year_end:828 傳 cycle.academic_year),
    #   函式內部 +1911 推算 proration / config_year / 學期區間。
    #   refresh_rates=True → 先回填 stored rates 再 derive_all(自動 special_bonus_items)。
    result = build_settlements(
        session,
        academic_year,
        included_resigned_ids=None,
        actor_id=actor_id,
        refresh_rates=True,
    )
    # build_settlements 只 flush 不 commit(交由 router transactional dep);此處 seedgen
    # 無 middleware,顯式 commit 落庫。
    session.commit()
    logger.info(
        "m13_year_end: build_settlements built=%d skipped_finalized=%d",
        result.built,
        result.skipped_finalized,
    )

    # --- 6) 把 DRAFT settlement 推進至 SUPERVISOR_SIGNED(鏡像 sign_supervisor) ---
    #   只動本 cycle、status==DRAFT 的列(idempotent:重跑不會二次簽)。
    signed_at = datetime.now(timezone.utc)
    draft_settlements = (
        session.query(YearEndSettlement)
        .filter(
            YearEndSettlement.year_end_cycle_id == cycle.id,
            YearEndSettlement.status == YearEndSettlementStatus.DRAFT,
        )
        .all()
    )
    for s in draft_settlements:
        s.status = YearEndSettlementStatus.SUPERVISOR_SIGNED
        s.supervisor_signed_by = actor_id
        s.supervisor_signed_at = signed_at
    session.commit()

    # log 結算單與快照筆數(以本 cycle 實際落庫數為準)。
    ctx.log("year_end_settlements", result.built)
    snapshot_count = (
        session.query(YearEndSettlement)
        .filter(YearEndSettlement.year_end_cycle_id == cycle.id)
        .count()
    )
    # employee_year_end_snapshot 與 settlement 1:1(每員工一筆),以 settlement 數計。
    ctx.log("employee_year_end_snapshot", snapshot_count)
