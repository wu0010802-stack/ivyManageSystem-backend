"""tests/test_year_end_auto_derive_integration.py — B7 整合測試（核心 gate）。

驗證 build_settlements(refresh_rates=True) 把 auto_derive（① ③ ④ ⑥）+ ⑤a 考勤扣款
wire 進結算流程，且**手動 override 不被自動覆寫**（最重要 gate）。

split-brain 註記：derive_festival_diff 內部 SalaryEngine(load_from_db=True) 會自開
session 讀 BonusConfig；test_db_session 把全域 _SessionFactory/engine swap 到 file-based
SQLite，故 reference data（BonusConfig/cycle/targets/students/registrations）必須在
build 前 **db.commit()**，才對 engine 自開的 session 可見（鏡像
test_year_end_auto_derive_festival_diff.py 的 seed commit 慣例）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as _D

import pytest
from sqlalchemy import select

from models.activity import ActivityCourse, ActivityRegistration, RegistrationCourse
from models.classroom import Classroom, Student
from models.config import BonusConfig, PositionSalaryConfig
from models.employee import Employee
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
)
from services.year_end import settlement_builder as sb

ACADEMIC_YEAR = 114
CYCLE_START = date(2025, 8, 1)
CYCLE_END = date(2026, 7, 31)
BONUS_CALC_DATE = date(2026, 1, 15)
CLASS_NAME = "大班A"
UNIT_PRICE = 100  # after_class_award_unit_price[大班A]


def _seed(db):
    """種一個 114 cycle，含：
      - 蔡宜倩（head_teacher_ab 班導，base 36160，帶「大班A」）
      - 大班A：ClassEnrollmentTarget（上/下）+ 3 名舊生 + 1 名新生（enrollment_school_year 非 NULL）
      - 上學期 5 筆 RegistrationCourse（matched，計入 J=5）→ AFTER_CLASS_AWARD = 5×100 = 500
      - BonusConfig 帶齊年終 Phase2 規則欄位（單價/紅利門檻/考勤費率）
      - OrgYearSettings（上/下）
    回 (cycle, emp, classroom)。
    """
    db.add(
        PositionSalaryConfig(
            head_teacher_a=39240, head_teacher_b=36160, head_teacher_c=33000
        )
    )
    db.add(
        BonusConfig(
            config_year=2025,
            version=1,
            is_active=True,
            head_teacher_ab=2000,
            head_teacher_c=1500,
            assistant_teacher_ab=1200,
            assistant_teacher_c=1200,
            principal_festival=6500,
            director_festival=3500,
            leader_festival=2000,
            driver_festival=1000,
            designer_festival=1000,
            admin_festival=2000,
            art_teacher_festival=2000,
            # 年終 Phase2 規則欄位（B1）
            after_class_award_unit_price={CLASS_NAME: UNIT_PRICE},
            dividend_returning_threshold=0.9,
            dividend_returning_amount=500,
            dividend_activity_threshold=0.8,
            dividend_activity_amount=1000,
            late_deduction_per_time=50,
            missing_punch_deduction_per_time=50,
            personal_leave_deduction_per_day=500,
            sick_leave_deduction_per_day=500,
        )
    )
    db.flush()

    cycle = YearEndCycle(
        academic_year=ACADEMIC_YEAR,
        start_date=CYCLE_START,
        end_date=CYCLE_END,
        bonus_calc_date=BONUS_CALC_DATE,
    )
    db.add(cycle)
    db.flush()

    classroom = Classroom(name=CLASS_NAME, school_year=114, semester=1)
    db.add(classroom)
    db.flush()

    emp = Employee(
        employee_id="E_TSAI",
        name="蔡宜倩",
        position="班導",
        bonus_grade="b",
        title="幼兒園教師",
        base_salary=36160,
        bypass_standard_base=False,
        is_active=True,
        classroom_id=classroom.id,
        hire_date=date(2020, 1, 1),
    )
    db.add(emp)
    db.flush()

    # OrgYearSettings（上/下）
    for sem_first, rate in ((True, _D("90.0")), (False, _D("90.0"))):
        db.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=sem_first,
                enrollment_target=20,
                school_achievement_rate=rate,
                org_achievement_rate=_D("0"),
            )
        )

    # ClassEnrollmentTarget（上/下）：head_count_target=4（→ 舊生率 3/4 = 0.75）
    for sem_first in (True, False):
        db.add(
            ClassEnrollmentTarget(
                year_end_cycle_id=cycle.id,
                semester_first=sem_first,
                classroom_id=classroom.id,
                head_teacher_employee_id=emp.id,
                head_count_target=4,
                class_performance_rate=_D("100.0"),
                returning_student_rate=_D("0.5"),  # 將被 B6 覆寫成 0.75
            )
        )

    # 學生：3 舊生（enrollment_school_year=113 < 114）+ 1 新生（=114）
    #   皆在籍（enrollment_date <= bonus_calc_date，無 graduation/withdrawal）
    #   enrollment_school_year 全非 NULL → B6 不走 fallback，無條件覆寫舊生率
    for i, sy in enumerate((113, 113, 113, 114)):
        db.add(
            Student(
                student_id=f"114-A-{i:03d}",
                name=f"幼生{i}",
                classroom_id=classroom.id,
                enrollment_date=date(2025, 8, 1),
                enrollment_school_year=sy,
                lifecycle_status="active",
            )
        )
    db.flush()

    # 上學期才藝報名：5 筆 RegistrationCourse（matched + enrolled）→ J=5
    course = ActivityCourse(
        name="畫畫班", price=500, is_active=True, school_year=114, semester=1
    )
    db.add(course)
    db.flush()
    for i in range(5):
        reg = ActivityRegistration(
            student_name=f"幼生{i}",
            is_active=True,
            school_year=114,
            semester=1,
            classroom_id=classroom.id,
            match_status="matched",
        )
        db.add(reg)
        db.flush()
        db.add(
            RegistrationCourse(
                registration_id=reg.id, course_id=course.id, status="enrolled"
            )
        )
    db.flush()
    return cycle, emp, classroom


def _settlement(db, cycle, emp):
    return db.scalar(
        select(YearEndSettlement).where(
            YearEndSettlement.year_end_cycle_id == cycle.id,
            YearEndSettlement.employee_id == emp.id,
        )
    )


def _special_items(db, cycle, emp, bonus_type):
    return list(
        db.scalars(
            select(SpecialBonusItem).where(
                SpecialBonusItem.year_end_cycle_id == cycle.id,
                SpecialBonusItem.employee_id == emp.id,
                SpecialBonusItem.bonus_type == bonus_type,
            )
        )
    )


class TestBuildAutoDerivesAll:
    """build_settlements(refresh_rates=True) 自動推導 ① ③ ④ ⑥ + ⑤a 扣款。"""

    def test_build_auto_derives_all(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed(db)
        db.commit()  # split-brain：festival_diff 自開 session 讀 config

        res = sb.build_settlements(
            db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True
        )
        db.commit()

        # ① AFTER_CLASS_AWARD 自動寫入（J=5 × K=100 = 500）
        aca = _special_items(db, cycle, emp, SpecialBonusType.AFTER_CLASS_AWARD)
        assert len(aca) == 1, f"應自動寫 1 筆 AFTER_CLASS_AWARD，got {len(aca)}"
        assert aca[0].amount == _D("500.00")
        assert (aca[0].source_ref or "").startswith("auto:")

        # ③ FESTIVAL_DIFF 自動寫入（金額不鎖，只確認有 auto 筆）
        fd = _special_items(db, cycle, emp, SpecialBonusType.FESTIVAL_DIFF)
        assert len(fd) == 1, f"應自動寫 1 筆 FESTIVAL_DIFF，got {len(fd)}"
        assert (fd[0].source_ref or "").startswith("auto:")

        # ④ SEMESTER_DIVIDEND_FIRST/SECOND 自動寫入
        sd_first = _special_items(
            db, cycle, emp, SpecialBonusType.SEMESTER_DIVIDEND_FIRST
        )
        sd_second = _special_items(
            db, cycle, emp, SpecialBonusType.SEMESTER_DIVIDEND_SECOND
        )
        assert len(sd_first) == 1 and len(sd_second) == 1
        assert (sd_first[0].source_ref or "").startswith("auto:")
        # 舊生率 0.75 < 0.9 門檻 → returning 段 0；才藝率 distinct 5 生報名(但只 4 在籍active？)
        #   分母=該班 active 學生數=4，distinct registered student_id 皆 NULL → 0 → activity 0
        #   故 dividend = 0（仍寫一筆 0，stale-safe）。不鎖具體值，只確認 auto 寫入。

        # ⑥ returning_student_rate 自動覆寫（3 舊生 / 4 編制 = 0.750）
        cts = db.scalars(
            select(ClassEnrollmentTarget).where(
                ClassEnrollmentTarget.year_end_cycle_id == cycle.id
            )
        ).all()
        for ct in cts:
            assert ct.returning_student_rate == _D(
                "0.750"
            ), f"B6 應覆寫舊生率為 0.750，got {ct.returning_student_rate}"

        # ⑤a 扣款：本 seed 無 Attendance/Leave/Meeting → 4 自動扣欄皆 0
        st = _settlement(db, cycle, emp)
        assert st.deduction_late == _D("0.00")
        assert st.deduction_personal_leave == _D("0.00")
        assert st.deduction_sick_leave == _D("0.00")
        assert st.deduction_meeting == _D("0.00")

        # derive_report 帶回
        assert res.derive_report is not None
        assert res.derive_report.unmatched_count >= 0


class TestManualOverrideNotClobberedByAuto:
    """核心 gate：手動扣款/手動 special bonus 不被 auto-derive 覆寫。"""

    def test_manual_override_not_clobbered_by_auto(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed(db)

        # 先建一個 DRAFT settlement（手動 disciplinary -6000；DB 欄位手動值）
        from models.year_end import (
            EmployeeYearEndSnapshot,
            YearEndSettlementStatus,
        )

        snap = EmployeeYearEndSnapshot(year_end_cycle_id=cycle.id, employee_id=emp.id)
        db.add(snap)
        db.flush()
        st = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snap.id,
            status=YearEndSettlementStatus.DRAFT,
            deduction_disciplinary=_D("-6000"),
        )
        db.add(st)

        # 手動 EXCESS_ENROLLMENT（source_ref=None → 手動筆，auto 不碰）
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                bonus_type=SpecialBonusType.EXCESS_ENROLLMENT,
                period_label="114上-manual",
                amount=_D("2000"),
                source_ref=None,
            )
        )
        # 手動 AFTER_CLASS_AWARD（同 auto 會寫的 uq 鍵 period_label）→ 必須命中 skip branch
        manual_aca_label = f"{ACADEMIC_YEAR}上-C{classroom.id}"
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                bonus_type=SpecialBonusType.AFTER_CLASS_AWARD,
                period_label=manual_aca_label,
                amount=_D("9999"),
                source_ref=None,  # 手動筆
            )
        )
        db.flush()
        db.commit()  # split-brain

        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True)
        db.commit()

        st = _settlement(db, cycle, emp)
        # 手動 disciplinary 存活（B5 不碰 disciplinary）
        assert st.deduction_disciplinary == _D(
            "-6000.00"
        ), f"手動 disciplinary 應存活，got {st.deduction_disciplinary}"

        # 手動 EXCESS_ENROLLMENT 那筆仍在、金額未變
        excess = _special_items(db, cycle, emp, SpecialBonusType.EXCESS_ENROLLMENT)
        assert len(excess) == 1
        assert excess[0].amount == _D("2000")

        # 手動 AFTER_CLASS_AWARD（同 uq 鍵）未被 auto 覆寫（命中 skip branch）
        aca = _special_items(db, cycle, emp, SpecialBonusType.AFTER_CLASS_AWARD)
        manual_rows = [r for r in aca if r.period_label == manual_aca_label]
        assert len(manual_rows) == 1
        assert manual_rows[0].amount == _D(
            "9999"
        ), f"手動 AFTER_CLASS_AWARD 應未被覆寫，got {manual_rows[0].amount}"
        assert (manual_rows[0].source_ref or "") == ""  # 仍是手動筆（None）


class TestDeductionOverrideRespected:
    """existing.calc_meta 有 deduction_late_override → 用 override 值非 B5 計算值。"""

    def test_deduction_override_respected(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed(db)

        from models.year_end import (
            EmployeeYearEndSnapshot,
            YearEndSettlementStatus,
        )

        snap = EmployeeYearEndSnapshot(year_end_cycle_id=cycle.id, employee_id=emp.id)
        db.add(snap)
        db.flush()
        st = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snap.id,
            status=YearEndSettlementStatus.DRAFT,
            calc_meta={"deduction_late_override": "-1234"},
        )
        db.add(st)
        db.flush()
        db.commit()

        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True)
        db.commit()

        st = _settlement(db, cycle, emp)
        # B5 計算值為 0（無 Attendance），但 override = -1234 應勝出
        assert st.deduction_late == _D(
            "-1234.00"
        ), f"deduction_late 應採 override -1234，got {st.deduction_late}"


class TestDeriveReportSurfaces:
    """build 回的 derive_report 帶 unmatched_count / fallback_classes。"""

    def test_derive_report_surfaces_unmatched_and_fallback(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed(db)
        db.commit()

        res = sb.build_settlements(
            db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True
        )
        db.commit()

        assert res.derive_report is not None
        assert res.derive_report.unmatched_count >= 0
        assert res.derive_report.fallback_classes >= 0


class TestGraduatedClassSchoolWideFallback:
    """Part 2c：帶班角色但無對應 ClassEnrollmentTarget（畢業班/班導已離職）→
    class_returning_rate 用全校舊生率 × 100；helper 回 None → 維持 None。

    直接測 gather_performance_rates（refresh_rates=False 路徑，不觸 derive_all），
    隔離 2c 邏輯。seed 全校：OrgYearSettings(target=20) + 4 在籍生（sy 113×3/114×1）
    → school_wide_returning_rate = 3/20 = 0.150 → ×100 = 15.0。
    """

    def _grad_teacher(self, db, cycle):
        """建一名「畢業班老師」：head_teacher_ab 角色（_has_class_role True），
        但無任何 head_teacher_employee_id 指向他的 ClassEnrollmentTarget。"""
        emp = Employee(
            employee_id="E_GRAD",
            name="畢業班老師",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=True,
            hire_date=date(2018, 1, 1),
        )
        db.add(emp)
        db.flush()
        return emp

    def test_grad_class_uses_school_wide_returning_rate(self, test_db_session):
        db = test_db_session
        cycle, _tsai, _classroom = _seed(db)
        grad = self._grad_teacher(db, cycle)

        # 確認 _has_class_role 為 True 且確實無對應 target（fallback 前提）
        assert sb._has_class_role(grad) is True
        rates = sb.gather_performance_rates(db, cycle, grad)

        # 全校舊生率 3/20 = 0.150 → ×100 = 15.0；上下學期皆 fallback
        assert rates.class_returning_rate_first == _D("15.000")
        assert rates.class_returning_rate_second == _D("15.000")
        # 無班 → 經營績效維持 None
        assert rates.class_performance_rate_first is None
        assert rates.class_performance_rate_second is None

    def test_grad_class_helper_none_stays_none(self, test_db_session):
        db = test_db_session
        cycle, _tsai, _classroom = _seed(db)
        grad = self._grad_teacher(db, cycle)

        # 讓 school_wide_returning_rate 回 None：刪掉 OrgYearSettings(semester_first=True)
        # （helper：OrgYearSettings 列缺 → None）
        org_first = db.scalar(
            select(OrgYearSettings).where(
                OrgYearSettings.year_end_cycle_id == cycle.id,
                OrgYearSettings.semester_first == True,  # noqa: E712
            )
        )
        db.delete(org_first)
        db.flush()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        assert school_wide_returning_rate(db, cycle) is None  # 前提：helper 回 None

        rates = sb.gather_performance_rates(db, cycle, grad)
        # helper None → fallback 不寫，class_returning_rate 維持 None
        assert rates.class_returning_rate_first is None
        assert rates.class_returning_rate_second is None


# =========================================================================== #
# P1-2 / P1-1 補修：finalized drift 護欄 + ③ 對齊 build 參與者（含 included_resigned）
# =========================================================================== #


class TestFinalizedItemsNotDriftedByRebuild:
    """P1-2（最高優先，原零覆蓋）：refresh_rates=True 的 re-build 不得覆寫
    finalized settlement 的底層 ①③④ auto items（凍結總額與 items 不漂移）。

    場景（常見 workflow）：HR 對某員工 finalize 後，又對其他 DRAFT 員工 re-build
    （refresh_rates=True 連帶跑 derive_all）。修補前 derive_all 不看 settlement.status，
    會覆寫**所有**員工（含 finalized）的 ①③④ auto items → finalized settlement 在 build
    loop 開頭被 skip（金額凍結）但底層 items 被改 → 漂移、稽核對不上。

    判別力設計：
      - finalized 員工（蔡宜倩）的 ①③④ auto items 預埋 derive **絕不會算出**的
        7777 值（① 真值 = 5×100 = 500；故 7777 一旦被覆寫成 500 即可偵測）。
      - frozen total_amount = 99999；finalized 在 loop 被 skip，total 不應變。
      - 另加一名 DRAFT 班導（另一班），證明 skip 是**選擇性**而非全域 no-op
        （DRAFT 者的 ① auto item 確實被寫入）。
    """

    def _draft_head_teacher_with_class(self, db, cycle):
        """另建一名 DRAFT 班導 + 其專屬班級（含 ClassEnrollmentTarget 上/下 + 單價 +
        2 筆 matched 報名 → ① = 2×100 = 200），用以證明 skip 為選擇性。"""
        from models.year_end import YearEndSettlementStatus  # noqa: F401

        # 該班需有 after_class_award 單價 → 補進現有 active BonusConfig 的 JSON。
        cfg = db.scalar(
            select(BonusConfig)
            .where(BonusConfig.is_active == True)  # noqa: E712
            .order_by(BonusConfig.id.desc())
        )
        prices = dict(cfg.after_class_award_unit_price or {})
        prices["中班B"] = UNIT_PRICE
        cfg.after_class_award_unit_price = prices

        cls2 = Classroom(name="中班B", school_year=114, semester=1)
        db.add(cls2)
        db.flush()
        emp2 = Employee(
            employee_id="E_DRAFT",
            name="林老師",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=True,
            classroom_id=cls2.id,
            hire_date=date(2019, 1, 1),
        )
        db.add(emp2)
        db.flush()
        for sem_first in (True, False):
            db.add(
                ClassEnrollmentTarget(
                    year_end_cycle_id=cycle.id,
                    semester_first=sem_first,
                    classroom_id=cls2.id,
                    head_teacher_employee_id=emp2.id,
                    head_count_target=4,
                    class_performance_rate=_D("100.0"),
                    returning_student_rate=_D("0.5"),
                )
            )
        course2 = ActivityCourse(
            name="音樂班", price=500, is_active=True, school_year=114, semester=1
        )
        db.add(course2)
        db.flush()
        for i in range(2):
            reg = ActivityRegistration(
                student_name=f"中{i}",
                is_active=True,
                school_year=114,
                semester=1,
                classroom_id=cls2.id,
                match_status="matched",
            )
            db.add(reg)
            db.flush()
            db.add(
                RegistrationCourse(
                    registration_id=reg.id, course_id=course2.id, status="enrolled"
                )
            )
        db.flush()
        return emp2, cls2

    def test_finalized_items_not_drifted_by_rebuild(self, test_db_session):
        db = test_db_session
        cycle, tsai, classroom = _seed(db)

        from models.year_end import (
            EmployeeYearEndSnapshot,
            YearEndSettlementStatus,
        )

        # ── 蔡宜倩：FINALIZED settlement + 預埋 ①③④ auto items（值=7777）+ 凍結 total ──
        snap = EmployeeYearEndSnapshot(year_end_cycle_id=cycle.id, employee_id=tsai.id)
        db.add(snap)
        db.flush()
        finalized = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=tsai.id,
            snapshot_id=snap.id,
            status=YearEndSettlementStatus.FINALIZED,
            total_amount=_D("99999"),
        )
        db.add(finalized)

        # ① auto item（period_label 與 derive 寫入鍵一致 → 若無 guard 必被覆寫成 500）
        aca_label = f"{ACADEMIC_YEAR}上-C{classroom.id}"
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=tsai.id,
                bonus_type=SpecialBonusType.AFTER_CLASS_AWARD,
                period_label=aca_label,
                amount=_D("7777"),
                classroom_id=classroom.id,
                source_ref="auto:after_class_award",  # auto 筆（非手動）→ 平常會被覆寫
            )
        )
        # ③ auto item
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=tsai.id,
                bonus_type=SpecialBonusType.FESTIVAL_DIFF,
                period_label=f"{ACADEMIC_YEAR}-FD",
                amount=_D("7777"),
                source_ref="auto:festival_diff",
            )
        )
        # ④ auto item（上學期）
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=tsai.id,
                bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
                period_label=f"{ACADEMIC_YEAR}上-C{classroom.id}",
                amount=_D("7777"),
                classroom_id=classroom.id,
                source_ref="auto:semester_dividend",
            )
        )

        # ── 另一名 DRAFT 班導（選擇性 skip 證明）──
        emp2, cls2 = self._draft_head_teacher_with_class(db, cycle)

        db.flush()
        db.commit()  # split-brain：festival_diff 自開 session 讀 config

        res = sb.build_settlements(
            db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True
        )
        db.commit()

        # (a) finalized settlement 在 loop 被 skip（金額凍結不變）
        assert res.skipped_finalized >= 1
        st = _settlement(db, cycle, tsai)
        assert st.status == YearEndSettlementStatus.FINALIZED
        assert st.total_amount == _D(
            "99999"
        ), f"finalized total 應凍結 99999，got {st.total_amount}"

        # (b) finalized 員工的底層 ①③④ auto items 未被覆寫（仍為 7777）
        aca = _special_items(db, cycle, tsai, SpecialBonusType.AFTER_CLASS_AWARD)
        assert len(aca) == 1 and aca[0].amount == _D(
            "7777"
        ), f"finalized ① 應未漂移（保持 7777），got {[r.amount for r in aca]}"
        fd = _special_items(db, cycle, tsai, SpecialBonusType.FESTIVAL_DIFF)
        assert len(fd) == 1 and fd[0].amount == _D(
            "7777"
        ), f"finalized ③ 應未漂移（保持 7777），got {[r.amount for r in fd]}"
        sd = _special_items(db, cycle, tsai, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert len(sd) == 1 and sd[0].amount == _D(
            "7777"
        ), f"finalized ④ 應未漂移（保持 7777），got {[r.amount for r in sd]}"

        # (c) 選擇性證明：DRAFT 班導的 ① auto item 確實被寫入（2×100 = 200）
        aca2 = _special_items(db, cycle, emp2, SpecialBonusType.AFTER_CLASS_AWARD)
        assert len(aca2) == 1 and aca2[0].amount == _D("200.00"), (
            f"DRAFT 班導 ① 應被寫入 200（skip 非全域 no-op），got "
            f"{[r.amount for r in aca2]}"
        )


class TestResignedIncludedFestivalDiffAligned:
    """P1-1：離職但 included 的班導應與 ①④ 一致地得到 ③ FESTIVAL_DIFF
    （build 參與者 = ACTIVE ∪ included_resigned_ids；③ 對齊）。

    修補前 ③ 只 iterate is_active==True → 離職 included 班導無 ③（與其 ①④ 不一致）。
    修補後 derive_all 把 included_resigned_ids 傳給 ③ → 其參與者含離職 included 班導。
    """

    def test_resigned_included_head_teacher_gets_festival_diff(self, test_db_session):
        db = test_db_session
        cycle, _tsai, _classroom = _seed(db)

        # 離職但 included 的班導：帶自己的班 + ClassEnrollmentTarget（上/下，target>0）。
        cls3 = Classroom(name="小班C", school_year=114, semester=1)
        db.add(cls3)
        db.flush()
        resigned = Employee(
            employee_id="E_RESIGNED",
            name="陳老師",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=False,  # 已離職
            classroom_id=cls3.id,
            hire_date=date(2015, 1, 1),
            resign_date=date(2025, 11, 30),  # cycle 期間內離職
        )
        db.add(resigned)
        db.flush()
        for sem_first in (True, False):
            db.add(
                ClassEnrollmentTarget(
                    year_end_cycle_id=cycle.id,
                    semester_first=sem_first,
                    classroom_id=cls3.id,
                    head_teacher_employee_id=resigned.id,
                    head_count_target=4,
                    class_performance_rate=_D("100.0"),
                    returning_student_rate=_D("0.5"),
                )
            )
        db.flush()
        db.commit()  # split-brain

        # included_resigned_ids 帶入該離職班導
        sb.build_settlements(
            db, ACADEMIC_YEAR, {resigned.id}, actor_id=1, refresh_rates=True
        )
        db.commit()

        # ③ FESTIVAL_DIFF：離職 included 班導應有一筆 auto（與 ①④ 一致）
        fd = _special_items(db, cycle, resigned, SpecialBonusType.FESTIVAL_DIFF)
        assert (
            len(fd) == 1
        ), f"離職 included 班導應得 ③ FESTIVAL_DIFF（對齊 build 參與者），got {len(fd)}"
        assert (fd[0].source_ref or "").startswith("auto:")

        # 對照：①④ 本就依 head_teacher 含離職班導（佐證一致性）
        aca = _special_items(db, cycle, resigned, SpecialBonusType.AFTER_CLASS_AWARD)
        sd = _special_items(
            db, cycle, resigned, SpecialBonusType.SEMESTER_DIVIDEND_FIRST
        )
        # ①：小班C 無單價設定 → derive skip（warning），故可能 0 筆；④ 無單價依賴 → 應有筆
        assert len(sd) == 1, f"離職 included 班導 ④ 應有筆，got {len(sd)}"
        # ① 視單價而定，不強斷言筆數（小班C 未設單價）；此測試核心是 ③ 對齊。
        _ = aca


class TestDeductionOverrideBeatsNonZeroB5:
    """P2 盲區修補：override 在 B5 計算值**非零**時仍勝出（判別力）。

    既有 TestDeductionOverrideRespected 的 B5 = 0（無 Attendance），override -1234 勝出
    無法區分「override 生效」與「B5 剛好 0」。本測試 seed 非零 Attendance（1 筆遲到 →
    B5 late = -50），再設 deduction_late_override="-1234" → 斷言 -1234（override 勝過
    非零 B5），證明 override 路徑真正生效。
    """

    def test_override_beats_nonzero_b5(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed(db)

        from models.attendance import Attendance
        from models.year_end import (
            EmployeeYearEndSnapshot,
            YearEndSettlementStatus,
        )

        # 非零 B5：1 筆遲到，落在民國 114 = 西元 2025 的扣款期間（Jan–Dec 2025）。
        # 費率 late_deduction_per_time=50（seed BonusConfig）→ B5 late = -50。
        db.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2025, 6, 15),
                is_late=True,
            )
        )

        snap = EmployeeYearEndSnapshot(year_end_cycle_id=cycle.id, employee_id=emp.id)
        db.add(snap)
        db.flush()
        st = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snap.id,
            status=YearEndSettlementStatus.DRAFT,
            calc_meta={"deduction_late_override": "-1234"},
        )
        db.add(st)
        db.flush()
        db.commit()

        # 先驗證 B5 計算值確為非零 -50（判別力前提：override 勝過的是非零值，非 0）
        from services.year_end.auto_derive.attendance_deductions import (
            derive_attendance_deductions,
        )

        auto = derive_attendance_deductions(db, cycle, emp)
        assert auto.late == _D(
            "-50.00"
        ), f"判別力前提：B5 應算出非零 -50，got {auto.late}"

        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True)
        db.commit()

        st = _settlement(db, cycle, emp)
        assert st.deduction_late == _D(
            "-1234.00"
        ), f"override -1234 應勝過非零 B5(-50)，got {st.deduction_late}"
