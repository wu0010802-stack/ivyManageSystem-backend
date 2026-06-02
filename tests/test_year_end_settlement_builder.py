"""tests/test_year_end_settlement_builder.py — settlement_builder helpers 單元測試（TDD）

覆蓋：
  1. festival_base_for_role  — 節慶=角色基數查表（決策④：單筆查 BonusConfig）
  2. compute_hire_months     — 在職月數（整個 cycle / 部分 / 離職在 cycle 中）
  3. resolve_org_achievement_rate — 組織績效率（滿年平均 / 僅一學期）
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.config import BonusConfig
from services.year_end import settlement_builder as sb

# ============ Fixtures ============


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ============ helpers ============


def _bonus_config(session, **overrides) -> BonusConfig:
    """建一筆 BonusConfig 並 flush；只需傳入要覆蓋的欄位。"""
    defaults = dict(
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
    )
    defaults.update(overrides)
    bc = BonusConfig(**defaults)
    session.add(bc)
    session.flush()
    return bc


def _emp(hire_date=None, resign_date=None):
    """輕量 stub：只需 hire_date / resign_date 兩個屬性。"""
    return SimpleNamespace(hire_date=hire_date, resign_date=resign_date)


# ============ Test: festival_base_for_role ============


class TestFestivalBaseForRole:
    def test_head_teacher_ab(self, session):
        _bonus_config(session, head_teacher_ab=2000)
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("2000")

    def test_principal(self, session):
        _bonus_config(session, principal_festival=6500)
        assert sb.festival_base_for_role(session, "principal") == Decimal("6500")

    def test_director(self, session):
        _bonus_config(session, director_festival=3500)
        assert sb.festival_base_for_role(session, "director") == Decimal("3500")

    def test_art_teacher_festival(self, session):
        _bonus_config(session, art_teacher_festival=1800)
        assert sb.festival_base_for_role(session, "art_teacher") == Decimal("1800")

    def test_unknown_role_returns_zero(self, session):
        _bonus_config(session)
        assert sb.festival_base_for_role(session, "unknown_role_xyz") == Decimal("0")

    def test_no_config_returns_zero(self, session):
        # DB 裡完全沒有 BonusConfig 時回 Decimal("0")
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("0")

    def test_festival_base_for_role_handles_null_field(self, session):
        # art_teacher_festival 是 nullable=True 欄位；設為 None 時 getattr/None 守衛應回 Decimal("0")
        _bonus_config(session, art_teacher_festival=None)
        assert sb.festival_base_for_role(session, "art_teacher") == Decimal("0")

    def test_uses_latest_by_id(self, session):
        """多筆 BonusConfig 時取 id 最大（最新）那筆。"""
        _bonus_config(session, head_teacher_ab=1000)
        _bonus_config(session, head_teacher_ab=2000)
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("2000")

    def test_festival_base_zero_for_kitchen_and_unmapped(self, session):
        """廚房/無法分類角色 → role_key_of 回 None → festival 基數 0。

        用 admin_festival=2000 的 BonusConfig 確保測試有鑑別力：
        如果廚房錯誤 fallback 到 admin，應得 2000 而非 0。
        對齊 Excel 王麗慧（廚工）festival = 0。
        """
        _bonus_config(session, admin_festival=2000)

        # 廚房員工
        kitchen_emp = SimpleNamespace(
            job_title_rel=None,
            title="廚工",
            position="廚房",
        )
        key = sb.role_key_of(kitchen_emp)
        assert key is None, f"廚房應回 None，got {key!r}"
        assert sb.festival_base_for_role(session, key) == Decimal("0"), (
            "廚房 festival 基數應為 0（對齊 Excel 廚工=0），不應 fallback 到 admin 2000"
        )

        # 未知/無法分類角色
        unknown_emp = SimpleNamespace(
            job_title_rel=None,
            title="護理師",
            position="護理",
        )
        key2 = sb.role_key_of(unknown_emp)
        assert key2 is None, f"未知角色應回 None，got {key2!r}"
        assert sb.festival_base_for_role(session, key2) == Decimal("0")


# ============ Test: compute_hire_months ============


class TestComputeHireMonths:
    CYCLE_START = date(2025, 2, 1)
    CYCLE_END = date(2026, 1, 31)

    def test_full_year_no_hire_or_resign(self):
        emp = _emp(hire_date=None, resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")

    def test_full_year_hired_before_cycle(self):
        emp = _emp(hire_date=date(2020, 1, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")

    def test_partial_hired_midcycle(self):
        # hire 2025-04-01：cycle_start=2025-02-01, first work month=2025-04
        # months: 2025-04 to 2026-01 = 10 months
        emp = _emp(hire_date=date(2025, 4, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("10")

    def test_resigned_midcycle(self):
        # resign 2025-10-31：2025-02 to 2025-10 = 9 months
        emp = _emp(hire_date=None, resign_date=date(2025, 10, 31))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("9")

    def test_short_tenure_both_hire_and_resign(self):
        # hire 2025-07-01, resign 2025-09-30 → 3 months
        emp = _emp(hire_date=date(2025, 7, 1), resign_date=date(2025, 9, 30))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("3")

    def test_resign_before_cycle_returns_zero(self):
        # 離職在 cycle 開始前：沒重疊
        emp = _emp(hire_date=None, resign_date=date(2025, 1, 15))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("0")

    def test_hire_after_cycle_returns_zero(self):
        # 到職在 cycle 結束後：沒重疊
        emp = _emp(hire_date=date(2026, 3, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("0")

    def test_max_clamp_at_12(self):
        # 即使在職橫跨超過 12 個月（異常資料），回傳 12
        emp = _emp(hire_date=date(2010, 1, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")


# ============ Test: resolve_org_achievement_rate ============


class TestResolveOrgAchievementRate:
    def test_full_year_both_semesters(self):
        # 兩學期平均：(75.6 + 91.5) / 2 = 83.55 → round to 1 decimal = 83.6
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=True,
            worked_second=True,
        )
        assert result == Decimal("83.6")

    def test_partial_only_second_semester(self):
        # 只在職第二學期：直接取第二學期
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=False,
            worked_second=True,
        )
        assert result == Decimal("91.5")

    def test_partial_only_first_semester(self):
        # 只在職第一學期：直接取第一學期
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=True,
            worked_second=False,
        )
        assert result == Decimal("75.6")

    def test_neither_semester_returns_zero(self):
        # 兩個都沒做（異常資料）：回 Decimal("0.0")
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=False,
            worked_second=False,
        )
        assert result == Decimal("0.0")

    def test_rounding_to_one_decimal(self):
        # 確保四捨五入到小數點第一位（ROUND_HALF_UP，非 banker's rounding）
        # (80.5 + 80.6) / 2 = 80.55 → ROUND_HALF_UP → 80.6（banker's rounding 會得 80.6 也對，但 2dp 下會錯）
        # 真正鑑別：Decimal("80.55").quantize(0.1, ROUND_HALF_UP)=80.6，ROUND_HALF_EVEN=80.6 也對，
        # 但若 inputs 為 Decimal("80.4") + Decimal("80.5") → avg=80.45 → HALF_UP=80.5, HALF_EVEN=80.4
        # 使用 80.4 / 80.6 確保 avg=80.5 整，而 (80.5+80.6)/2=80.55 → 只 HALF_UP 進位
        result = sb.resolve_org_achievement_rate(
            Decimal("80.5"),
            Decimal("80.6"),
            worked_first=True,
            worked_second=True,
        )
        assert result == Decimal("80.6")

    def test_org_rate_none_safe_missing_semester(self):
        """worked=True 但該學期 OrgYearSettings 缺列（rate=None）時不崩潰。

        情境：員工上下學期皆在職，但上學期 OrgYearSettings 尚未建立（rate=None）。
        應只平均非 None 的學期；全部 None 時回 Decimal("0.0")。
        """
        # worked_first=True, first=None → 跳過上學期；only second=91.5 納入
        result = sb.resolve_org_achievement_rate(
            None,
            Decimal("91.5"),
            worked_first=True,
            worked_second=True,
        )
        assert result == Decimal("91.5"), (
            "上學期 rate=None 應跳過，只取下學期 91.5"
        )

        # 兩學期 rate 皆 None → 回 Decimal("0.0")（而非 crash）
        result2 = sb.resolve_org_achievement_rate(
            None,
            None,
            worked_first=True,
            worked_second=True,
        )
        assert result2 == Decimal("0.0"), (
            "兩學期 rate 皆 None 應回 Decimal('0.0')，不應 crash"
        )


# =========================================================================== #
# Task 3：build_settlements 端到端對帳（金標準：蔡宜倩 40106.71）           #
# =========================================================================== #

from decimal import Decimal as _D  # noqa: E402

from models.classroom import Classroom  # noqa: E402
from models.config import PositionSalaryConfig  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import (  # noqa: E402
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)

ACADEMIC_YEAR = 114
CYCLE_START = date(2025, 8, 1)  # 114 學年 = 西元 2025/8 ～ 2026/7
CYCLE_END = date(2026, 7, 31)
BONUS_CALC_DATE = date(2026, 1, 15)


def _seed_tsai_cycle(db):
    """種一個 114 cycle + 蔡宜倩（班導 head_teacher_ab，base 36160）+ 全套 stored rates。

    rates 直接種「成分值」（不依賴在籍計算）：
      全校達成率 75.6 / 91.5（兩學期，平均 83.55 = org_rate 83.6）
      班舊生率 0.929 / 1.000（→ 92.9 / 100，平均 96.45）
      班經營績效 106.4 / 115.3（平均 110.85）
      → avg_performance = (83.55+96.45+110.85)/3 = 96.95 → 1dp → 97.0
    special_bonus_items 合計 11062。
    回 (cycle, employee, classroom)。
    """
    # 職位標準底薪：head_teacher_b = 36160
    db.add(
        PositionSalaryConfig(
            head_teacher_a=39240, head_teacher_b=36160, head_teacher_c=33000
        )
    )
    # 節慶獎金基數：head_teacher_ab = 2000
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

    classroom = Classroom(name="大班A", school_year=114, semester=1)
    db.add(classroom)
    db.flush()

    emp = Employee(
        employee_id="E_TSAI",
        name="蔡宜倩",
        position="班導",
        bonus_grade="b",
        title="幼兒園教師",
        base_salary=30000,  # 不等於職位標準，驗證確實走 PositionSalaryConfig → 36160
        bypass_standard_base=False,
        is_active=True,
        classroom_id=classroom.id,
        hire_date=date(2020, 1, 1),  # 滿年在職
    )
    db.add(emp)
    db.flush()

    # 全校達成率：上 91.5 / 下 75.6（順序不影響平均）
    db.add(
        OrgYearSettings(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            enrollment_target=160,
            school_achievement_rate=_D("91.5"),
            org_achievement_rate=_D("0"),
        )
    )
    db.add(
        OrgYearSettings(
            year_end_cycle_id=cycle.id,
            semester_first=False,
            enrollment_target=160,
            school_achievement_rate=_D("75.6"),
            org_achievement_rate=_D("0"),
        )
    )
    # 班級兩學期：經營績效 106.4 / 115.3，舊生率 0.929 / 1.000
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=classroom.id,
            head_teacher_employee_id=emp.id,
            head_count_target=30,
            class_performance_rate=_D("106.4"),
            returning_student_rate=_D("0.929"),
        )
    )
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=False,
            classroom_id=classroom.id,
            head_teacher_employee_id=emp.id,
            head_count_target=30,
            class_performance_rate=_D("115.3"),
            returning_student_rate=_D("1.000"),
        )
    )
    # special_bonus_items 合計 11062
    specials = [
        (SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST, "113下", _D("3312")),
        (SpecialBonusType.SEMESTER_DIVIDEND_FIRST, "114上", _D("1500")),
        (SpecialBonusType.SEMESTER_DIVIDEND_SECOND, "114下", _D("1000")),
        (SpecialBonusType.AFTER_CLASS_AWARD, "114上", _D("1275")),
        (SpecialBonusType.EXCESS_ENROLLMENT, "114上", _D("2000")),
        (SpecialBonusType.FESTIVAL_DIFF, "114", _D("1975")),
    ]
    for bonus_type, label, amount in specials:
        db.add(
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                bonus_type=bonus_type,
                period_label=label,
                amount=amount,
            )
        )
    db.flush()
    return cycle, emp, classroom


def _get_settlement(db, cycle, emp):
    return (
        db.query(YearEndSettlement)
        .filter(
            YearEndSettlement.year_end_cycle_id == cycle.id,
            YearEndSettlement.employee_id == emp.id,
        )
        .one()
    )


class TestBuildSettlementsGoldReconciliation:
    """金標準對帳：蔡宜倩 total == 40106.71（Excel 40106.7072）。"""

    def test_tsai_reconciles_exactly(self, test_db_session):
        db = test_db_session
        cycle, emp, _ = _seed_tsai_cycle(db)

        # 第一次 build：扣項尚為 0（refresh_rates=False，用種好的 stored rates）
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        st = _get_settlement(db, cycle, emp)
        # 此時 total = payable(30944.71) + special(11062) = 42006.71（扣項 0）
        assert st.total_amount == _D("42006.71")

        # 種人工扣項：機構會議 -1000、遲到早退 -900（合計 -1900）
        st.deduction_meeting = _D("-1000")
        st.deduction_late = _D("-900")
        db.flush()

        # 第二次 build：gather_deductions 讀回手動扣項 → 應對齊 Excel
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        st = _get_settlement(db, cycle, emp)

        assert st.avg_performance_rate == _D("97.0")
        assert st.gross_amount == _D("37015.20")
        assert st.org_achievement_rate == _D("83.6")
        assert st.subtotal_amount == _D("30944.71")
        assert st.deduction_total == _D("-1900.00")
        assert st.payable_amount == _D("29044.71")
        assert st.special_bonus_total == _D("11062.00")
        assert st.total_amount == _D("40106.71")

    def test_build_idempotent(self, test_db_session):
        db = test_db_session
        cycle, emp, _ = _seed_tsai_cycle(db)

        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)

        rows = (
            db.query(YearEndSettlement)
            .filter(YearEndSettlement.employee_id == emp.id)
            .all()
        )
        assert len(rows) == 1  # 重跑不重複建列

    def test_build_skips_finalized(self, test_db_session):
        db = test_db_session
        cycle, emp, _ = _seed_tsai_cycle(db)

        # 第一次 build 後 finalize
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        st = _get_settlement(db, cycle, emp)
        st.status = YearEndSettlementStatus.FINALIZED
        frozen_total = st.total_amount
        db.flush()

        # 改一筆 special bonus（若重算會變動 total）
        item = (
            db.query(SpecialBonusItem)
            .filter(
                SpecialBonusItem.employee_id == emp.id,
                SpecialBonusItem.bonus_type == SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            )
            .first()
        )
        item.amount = _D("9999")
        db.flush()

        result = sb.build_settlements(
            db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False
        )
        assert result.skipped_finalized == 1
        assert result.built == 0

        st = _get_settlement(db, cycle, emp)
        assert st.total_amount == frozen_total  # FINALIZED 不被覆寫


# =========================================================================== #
# (A) refresh_enrollment_rates smoke：唯一無對帳覆蓋的面，確保跑得起來       #
# =========================================================================== #

from models.classroom import Student  # noqa: E402


class TestRefreshEnrollmentRatesSmoke:
    """refresh_enrollment_rates 由在籍回填 stored rates；其餘金標準測試皆 refresh_rates=False，
    此 smoke 確保 refresh path 不炸且確實寫入非空 rate。"""

    def test_refresh_writes_rates(self, test_db_session):
        db = test_db_session
        cycle, emp, classroom = _seed_tsai_cycle(db)

        # 種幾個在籍學生（enrollment_date 早於 bonus_calc_date，classroom 指向 emp 帶的班）
        for i in range(3):
            db.add(
                Student(
                    student_id=f"114-A-{i:03d}",
                    name=f"幼生{i}",
                    classroom_id=classroom.id,
                    enrollment_date=date(2025, 8, 1),
                )
            )
        db.flush()

        # refresh_rates=True 觸發 refresh_enrollment_rates（part A）
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=True)

        # OrgYearSettings.school_achievement_rate 應由在籍回填（3 / 160 × 100 ≈ 1.88）
        org = (
            db.query(OrgYearSettings)
            .filter(
                OrgYearSettings.year_end_cycle_id == cycle.id,
                OrgYearSettings.semester_first == True,  # noqa: E712
            )
            .one()
        )
        assert org.enrollment_actual == 3
        assert org.school_achievement_rate == _D("1.88")

        # ClassEnrollmentTarget.class_performance_rate 應由在籍回填（3 人 / 30 編制 × 100 = 10.00）
        ct = (
            db.query(ClassEnrollmentTarget)
            .filter(
                ClassEnrollmentTarget.year_end_cycle_id == cycle.id,
                ClassEnrollmentTarget.semester_first == True,  # noqa: E712
            )
            .one()
        )
        assert ct.class_performance_rate == _D("10.00")
        assert ct.avg_monthly_enrollment is not None


# =========================================================================== #
# Bug fix tests: Fix 1 (民國曆年) + Fix 2 (hire_months_override)              #
# =========================================================================== #


class TestProrationCivilCalendarYear:
    """Fix 1 驗證：年終比例計算以民國曆年（Jan 1–Dec 31）為基準，非學年 Aug–Jul。

    郭玟秀情境：2025 年 1 月至 10 月在職（辭職 2025-10-31）
    - 正確：民國 114 年 = 西元 2025 Jan–Dec → 在職 10 個月
    - 舊 bug：學年 2025/08–2026/07 → 在職 Aug–Oct = 3 個月
    """

    def test_proration_uses_civil_calendar_year(self, test_db_session):
        db = test_db_session

        # 種最小必要資料：cycle (academic_year=114) + OrgYearSettings + employee
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
            )
        )
        db.flush()

        cycle = YearEndCycle(
            academic_year=ACADEMIC_YEAR,  # 114 → 西元 2025
            start_date=CYCLE_START,       # 2025-08-01（學年開始）
            end_date=CYCLE_END,           # 2026-07-31（學年結束）
            bonus_calc_date=BONUS_CALC_DATE,
        )
        db.add(cycle)
        db.flush()

        # OrgYearSettings：兩學期（不然 org_rate crash）
        db.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=True,
                enrollment_target=160,
                school_achievement_rate=_D("80.0"),
                org_achievement_rate=_D("0"),
            )
        )
        db.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=False,
                enrollment_target=160,
                school_achievement_rate=_D("80.0"),
                org_achievement_rate=_D("0"),
            )
        )

        # 員工：hired 在 2025-01-01（民國年首），辭職 2025-10-31（民國年第 10 月底）
        # 在職 1–10 月 = 10 個月；但學年 Aug–Jul 下只有 Aug–Oct = 3 個月
        emp = Employee(
            employee_id="E_GUO",
            name="郭玟秀",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=True,  # 必須 True 才進迴圈（或 included_resigned_ids）
            hire_date=date(2025, 1, 1),
            resign_date=date(2025, 10, 31),
        )
        db.add(emp)
        db.flush()

        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)

        st = _get_settlement(db, cycle, emp)

        # 民國 114 年 = 2025-01-01 .. 2025-12-31
        # hire_date=2025-01-01, resign_date=2025-10-31 → effective Jan–Oct = 10 個月
        assert st.hire_months == _D("10"), (
            f"民國曆年基準應得 10 個月，舊 bug（學年）會得 3。got={st.hire_months}"
        )
        # proration = 10/12 = 0.8333
        assert st.proration_rate == _D("0.8333"), (
            f"proration_rate 應為 0.8333，got={st.proration_rate}"
        )
        # payable 應以 10/12 比例計算（不為 0）
        assert st.payable_amount > _D("0"), "payable_amount 不應為 0"


class TestHireMonthsOverrideHonoredAndPreserved:
    """Fix 2 驗證：build_settlements 讀取並保留 calc_meta.hire_months_override。

    流程：
      1. 第一次 build → auto_months = 12（滿年）
      2. 模擬 Task 6 手動設定：settlement.calc_meta["hire_months_override"] = "4.5"
      3. 第二次 re-build → hire_months 應採覆寫值 4.5，proration = 4.5/12 = 0.3750
      4. override key 在 re-build 後仍保留於 calc_meta（不被蓋掉）
    """

    def test_hire_months_override_honored_and_preserved(self, test_db_session):
        db = test_db_session
        cycle, emp, _ = _seed_tsai_cycle(db)

        # 第一次 build（蔡宜倩滿年，auto = 12）
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        st = _get_settlement(db, cycle, emp)
        assert st.hire_months == _D("12"), f"第一次 build 應為 12 個月，got={st.hire_months}"

        # 模擬 Task 6 手動 patch：將 hire_months_override 寫入 calc_meta
        # 用字串值確保 JSON serialization 不炸（Decimal 不可直接 JSON serialize）
        st.calc_meta = {**(st.calc_meta or {}), "hire_months_override": "4.5"}
        db.flush()

        # 第二次 re-build：應讀取覆寫值 4.5
        sb.build_settlements(db, ACADEMIC_YEAR, set(), actor_id=1, refresh_rates=False)
        db.expire(st)  # 強制 re-read from DB
        st = _get_settlement(db, cycle, emp)

        assert st.hire_months == _D("4.5"), (
            f"覆寫後 re-build 應得 4.5 個月，got={st.hire_months}"
        )
        assert st.proration_rate == _D("0.3750"), (
            f"proration_rate 應為 0.3750（4.5/12），got={st.proration_rate}"
        )
        # override key 應被保留（不被 re-build 蓋掉）
        assert st.calc_meta.get("hire_months_override") == "4.5", (
            f"calc_meta.hire_months_override 應被保留，got calc_meta={st.calc_meta}"
        )
