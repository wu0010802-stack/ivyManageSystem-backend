"""tests/test_year_end_auto_derive_returning_rate.py — B6 班級舊生率自動推導（TDD）

覆蓋：
  1. 主測：舊生數/編制 → returning_student_rate（如 22/24=0.917），Decimal 3dp。
  2. fallback：班內任一在籍學生 enrollment_school_year IS NULL → 不寫該班 + fallback_classes==1。
  3. 全校率 helper：school_wide_returning_rate = 全校舊生/全校目標；全校有 NULL → None。
  4. 邊界：enrollment_school_year == academic_year（新生不算）；> academic_year（不算）。
  5. sabotage：分母誤用在籍總數而非編制 → 斷言 FAIL（confirm 用 head_count_target）。

核心算法（recon「班級經營績效」舊生預繳率）：
  舊生 = 在籍學生中 enrollment_school_year < cycle.academic_year。
  舊生率 = 舊生數 / head_count_target（編制，非在籍總數！）。
  在籍基準日 = cycle.bonus_calc_date（對齊 settlement_builder.refresh_enrollment_rates）。
  在籍判定 = enrollment_rates._enrolled_on_filter（純日期，不依賴 lifecycle）。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    YearEndCycle,
)

# 學年（民國）114：上學期 114/8 ～ 115/1，基準日 115/1/15。
_ACADEMIC_YEAR = 114
_BASIS = date(2026, 1, 15)  # bonus_calc_date

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


@pytest.fixture
def cycle(session):
    c = YearEndCycle(
        academic_year=_ACADEMIC_YEAR,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=_BASIS,
    )
    session.add(c)
    session.flush()
    return c


def _make_classroom(session, name: str) -> Classroom:
    c = Classroom(name=name, school_year=_ACADEMIC_YEAR, semester=1)
    session.add(c)
    session.flush()
    return c


def _make_target(
    session,
    cycle,
    classroom_id: int,
    *,
    head_count_target: int,
    semester_first: bool = True,
    returning_student_rate: Decimal = Decimal("0"),
) -> ClassEnrollmentTarget:
    t = ClassEnrollmentTarget(
        year_end_cycle_id=cycle.id,
        semester_first=semester_first,
        classroom_id=classroom_id,
        head_count_target=head_count_target,
        returning_student_rate=returning_student_rate,
    )
    session.add(t)
    session.flush()
    return t


def _make_student(
    session,
    *,
    classroom_id: int,
    enrollment_school_year: int | None,
    enrollment_date: date | None = date(2025, 9, 1),
    graduation_date: date | None = None,
    withdrawal_date: date | None = None,
    lifecycle_status: str = LIFECYCLE_ACTIVE,
) -> Student:
    s = Student(
        student_id=f"T{session.query(Student).count() + 1:04d}",
        name="測試生",
        classroom_id=classroom_id,
        enrollment_school_year=enrollment_school_year,
        enrollment_date=enrollment_date,
        graduation_date=graduation_date,
        withdrawal_date=withdrawal_date,
        lifecycle_status=lifecycle_status,
    )
    session.add(s)
    session.flush()
    return s


def _make_org(
    session, cycle, *, enrollment_target: int, semester_first: bool = True
) -> OrgYearSettings:
    o = OrgYearSettings(
        year_end_cycle_id=cycle.id,
        semester_first=semester_first,
        enrollment_target=enrollment_target,
    )
    session.add(o)
    session.flush()
    return o


# ============ Test: 主測（舊生/編制） ============


class TestReturningRate:
    def test_returning_rate_old_over_target(self, session, cycle):
        """天堂鳥：編制 24；在籍 22 舊生（< 114）+ 2 新生（== 114）→ 22/24 = 0.917。"""
        c = _make_classroom(session, "天堂鳥")
        _make_target(session, cycle, c.id, head_count_target=24)

        # 22 名舊生（enrollment_school_year < 114）
        for _ in range(22):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        # 2 名新生（== 114，不算舊生）
        for _ in range(2):
            _make_student(session, classroom_id=c.id, enrollment_school_year=114)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 22/24 = 0.91666... → ROUND_HALF_UP 3dp → 0.917
        assert tgt.returning_student_rate == Decimal(
            "0.917"
        ), f"expected 0.917, got {tgt.returning_student_rate!r}"
        assert report.written == 1
        assert report.fallback_classes == 0

    def test_returning_rate_full_class_overwrites_manual(self, session, cycle):
        """完整班（無 NULL）→ 無條件覆寫既有手填值（Phase1 手填 → Phase2 自動）。"""
        c = _make_classroom(session, "茉莉")
        # 既有手填 0.5（將被覆寫）
        _make_target(
            session,
            cycle,
            c.id,
            head_count_target=10,
            returning_student_rate=Decimal("0.500"),
        )
        for _ in range(10):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 10/10 = 1.000，覆寫 0.500
        assert tgt.returning_student_rate == Decimal("1.000")

    def test_returning_rate_excludes_withdrawn_from_numerator(self, session, cycle):
        """基準日前已退學的舊生不計入分子（在籍判定用純日期 filter）。"""
        c = _make_classroom(session, "牡丹")
        _make_target(session, cycle, c.id, head_count_target=10)
        # 8 在籍舊生
        for _ in range(8):
            _make_student(session, classroom_id=c.id, enrollment_school_year=112)
        # 1 名舊生在基準日前退學（withdrawal_date <= basis）→ 不在籍 → 不計
        _make_student(
            session,
            classroom_id=c.id,
            enrollment_school_year=112,
            withdrawal_date=date(2025, 12, 1),
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 8/10 = 0.800（退學生不計）
        assert tgt.returning_student_rate == Decimal("0.800")


# ============ Test: 兩學期兩列 ============


class TestBothSemesters:
    def test_writes_both_semester_rows(self, session, cycle):
        """同班 semester_first True/False 兩列各算各寫（同基準日 → 同值）。"""
        c = _make_classroom(session, "薔薇")
        _make_target(session, cycle, c.id, head_count_target=24, semester_first=True)
        _make_target(session, cycle, c.id, head_count_target=24, semester_first=False)
        for _ in range(23):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        for _ in range(1):
            _make_student(session, classroom_id=c.id, enrollment_school_year=114)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        rows = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).all()
        assert len(rows) == 2
        # 23/24 = 0.95833... → 0.958
        for r in rows:
            assert r.returning_student_rate == Decimal("0.958")
        assert report.written == 2


# ============ Test: fallback（NULL enrollment_school_year） ============


class TestFallback:
    def test_null_enrollment_year_triggers_fallback(self, session, cycle):
        """班內任一在籍學生 enrollment_school_year IS NULL → 不寫該班，保留既有手填。"""
        c = _make_classroom(session, "百合")
        _make_target(
            session,
            cycle,
            c.id,
            head_count_target=24,
            returning_student_rate=Decimal("0.917"),  # 既有手填，須保留
        )
        # 21 舊生 + 1 名 NULL（prod backfill 未完成）
        for _ in range(21):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        _make_student(session, classroom_id=c.id, enrollment_school_year=None)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 手填值保留（不寫半套）
        assert tgt.returning_student_rate == Decimal("0.917")
        assert report.fallback_classes == 1
        assert report.written == 0

    def test_withdrawn_null_does_not_trigger_fallback(self, session, cycle):
        """已退學（不在籍）學生即使 enrollment_school_year NULL，也不觸發 fallback。"""
        c = _make_classroom(session, "櫻花")
        _make_target(session, cycle, c.id, head_count_target=10)
        for _ in range(10):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        # 一名「已退學 + NULL」的學生：不在籍 → 不應觸發 fallback
        _make_student(
            session,
            classroom_id=c.id,
            enrollment_school_year=None,
            withdrawal_date=date(2025, 10, 1),
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 10/10 = 1.000（退學 NULL 生不在籍 → 不算分子也不觸發 fallback）
        assert tgt.returning_student_rate == Decimal("1.000")
        assert report.fallback_classes == 0
        assert report.written == 1


# ============ Test: 邊界（新生 / 未來學年） ============


class TestBoundary:
    def test_same_year_is_new_student_not_counted(self, session, cycle):
        """enrollment_school_year == academic_year（新生）不算舊生。"""
        c = _make_classroom(session, "向日葵")
        _make_target(session, cycle, c.id, head_count_target=10)
        # 全部 == 114 新生 → 0 舊生
        for _ in range(10):
            _make_student(session, classroom_id=c.id, enrollment_school_year=114)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        assert tgt.returning_student_rate == Decimal("0.000")

    def test_future_year_not_counted_as_old(self, session, cycle):
        """enrollment_school_year > academic_year（不應發生）不算舊生。"""
        c = _make_classroom(session, "滿天星")
        _make_target(session, cycle, c.id, head_count_target=10)
        for _ in range(5):
            _make_student(
                session, classroom_id=c.id, enrollment_school_year=113
            )  # 舊生
        for _ in range(3):
            _make_student(
                session, classroom_id=c.id, enrollment_school_year=115
            )  # 未來（不算舊生）
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 5 舊生 / 10 編制 = 0.500
        assert tgt.returning_student_rate == Decimal("0.500")

    def test_target_zero_skips(self, session, cycle):
        """head_count_target <= 0 → 不除零，不寫，記 fallback。"""
        c = _make_classroom(session, "零編制班")
        _make_target(
            session,
            cycle,
            c.id,
            head_count_target=0,
            returning_student_rate=Decimal("0.123"),
        )
        for _ in range(3):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 不寫（除零保護）；保留既有值
        assert tgt.returning_student_rate == Decimal("0.123")
        assert report.fallback_classes == 1
        assert report.written == 0


# ============ Test: sabotage（分母誤用在籍總數） ============


class TestSabotage:
    def test_denominator_is_target_not_enrolled_total(self, session, cycle):
        """編制 24、在籍 22（全舊生）→ 22/24=0.917；若誤用在籍總數 22 當分母 → 1.000。

        斷言結果為 0.917（用編制），而非 1.000（誤用在籍總數）。
        """
        c = _make_classroom(session, "天堂鳥B")
        _make_target(session, cycle, c.id, head_count_target=24)
        for _ in range(22):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        assert tgt.returning_student_rate == Decimal(
            "0.917"
        ), "分母必須是 head_count_target(24)，非在籍總數(22)"
        assert tgt.returning_student_rate != Decimal(
            "1.000"
        ), "誤用在籍總數當分母（22/22=1.000）→ sabotage 命中"


# ============ Test: 全校率 helper ============


class TestSchoolWideReturningRate:
    def test_school_wide_rate(self, session, cycle):
        """全校舊生 / 全校目標；多班學生彙總。"""
        c1 = _make_classroom(session, "班A")
        c2 = _make_classroom(session, "班B")
        _make_org(session, cycle, enrollment_target=160)
        # 班A：30 舊生；班B：50 舊生 → 全校 80 舊生 / 160 = 0.500
        for _ in range(30):
            _make_student(session, classroom_id=c1.id, enrollment_school_year=113)
        for _ in range(50):
            _make_student(session, classroom_id=c2.id, enrollment_school_year=112)
        # 一些新生（不算舊生）
        for _ in range(10):
            _make_student(session, classroom_id=c1.id, enrollment_school_year=114)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        rate = school_wide_returning_rate(session, cycle)
        # 80 / 160 = 0.500
        assert rate == Decimal("0.500")

    def test_school_wide_rate_null_returns_none(self, session, cycle):
        """全校任一在籍學生 enrollment_school_year NULL → 回 None（None-safe）。"""
        c = _make_classroom(session, "班C")
        _make_org(session, cycle, enrollment_target=160)
        for _ in range(50):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        _make_student(session, classroom_id=c.id, enrollment_school_year=None)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        assert school_wide_returning_rate(session, cycle) is None

    def test_school_wide_rate_no_org_returns_none(self, session, cycle):
        """無 OrgYearSettings 列 → 回 None。"""
        c = _make_classroom(session, "班D")
        for _ in range(10):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        assert school_wide_returning_rate(session, cycle) is None

    def test_school_wide_rate_target_zero_returns_none(self, session, cycle):
        """enrollment_target <= 0 → 回 None（除零保護）。"""
        c = _make_classroom(session, "班E")
        _make_org(session, cycle, enrollment_target=0)
        for _ in range(10):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        assert school_wide_returning_rate(session, cycle) is None

    def test_school_wide_withdrawn_null_does_not_block(self, session, cycle):
        """已退學（不在籍）NULL 生不阻斷全校率（NULL 檢查只看在籍集）。"""
        c = _make_classroom(session, "班F")
        _make_org(session, cycle, enrollment_target=160)
        for _ in range(80):
            _make_student(session, classroom_id=c.id, enrollment_school_year=113)
        # 退學 + NULL：不在籍 → 不阻斷
        _make_student(
            session,
            classroom_id=c.id,
            enrollment_school_year=None,
            withdrawal_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        # 80/160 = 0.500（退學 NULL 不阻斷、不計分子）
        assert school_wide_returning_rate(session, cycle) == Decimal("0.500")

    def test_school_wide_rate_none_when_zero_enrolled(self, session, cycle):
        """全校基準日 0 在籍（org target>0、無任何在籍學生）→ school_wide_returning_rate 回 None。

        P2-1：total==0 時現況會 0/target → 0.000；修後應回 None（與其他 None-safe 路徑一致，
        確保 B7 畢業班老師消費此 helper 時能正確 fallback 而非誤拿 0.000）。
        """
        # 有 OrgYearSettings（target>0）但基準日全校零在籍
        _make_org(session, cycle, enrollment_target=200)
        # 加一名在基準日前已退學的學生（不在籍）
        c = _make_classroom(session, "全退班G")
        _make_student(
            session,
            classroom_id=c.id,
            enrollment_school_year=113,
            withdrawal_date=date(2025, 10, 1),
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            school_wide_returning_rate,
        )

        assert school_wide_returning_rate(session, cycle) is None


# ============ Test: 零在籍班保留手填 ============


class TestZeroEnrolled:
    def test_zero_enrolled_class_keeps_manual_rate(self, session, cycle):
        """P1-1：班在基準日零在籍（head_count_target>0）→ fallback_classes==1，手填值保留。

        現況：total=0, null_count=0 → 不觸發 null_count>0 守衛 → 落入完整班覆寫分支
        寫 0/target=0.000，把合法手填清成 0（少發績效）。
        修後：total<=0 應在 null_count 守衛之前觸發 fallback（保留既有手填，不寫 0.000）。
        """
        c = _make_classroom(session, "空班H")
        # 既有手填 0.950（模擬 Phase1 HR 手填）
        _make_target(
            session,
            cycle,
            c.id,
            head_count_target=24,
            returning_student_rate=Decimal("0.950"),
        )
        # 基準日全班零在籍（例如開學前或所有學生均已提前退學）
        _make_student(
            session,
            classroom_id=c.id,
            enrollment_school_year=113,
            withdrawal_date=date(2025, 10, 1),  # 基準日 2026-01-15 前已退
            lifecycle_status=LIFECYCLE_WITHDRAWN,
        )
        session.commit()

        from services.year_end.auto_derive.returning_rate import (
            derive_returning_rate,
        )

        report = derive_returning_rate(session, cycle)
        session.commit()

        tgt = session.query(ClassEnrollmentTarget).filter_by(classroom_id=c.id).one()
        # 手填值必須保留（不被寫成 0.000）
        assert tgt.returning_student_rate == Decimal(
            "0.950"
        ), f"手填值被覆蓋！got {tgt.returning_student_rate!r}，應保留 0.950"
        assert (
            report.fallback_classes == 1
        ), f"fallback_classes={report.fallback_classes}，應==1"
        assert report.written == 0, f"written={report.written}，應==0（不應寫入空班）"
