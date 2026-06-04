"""TDD: 帶班人數改自動 (CLASS_HEADCOUNT_BONUS manual→auto, 超編制 × +2)。

驗證：
1. aggregate_cycle_status 正確計算 headcount_over_target（查年終 ClassEnrollmentTarget）
2. 未設定編制時 fallback 為 0（不崩潰）
3. rule_applier._apply_auto_item 針對 CLASS_HEADCOUNT_BONUS 產出正確 delta
4. CLASS_HEADCOUNT_BONUS 已加入 AUTO_ITEM_CODES

設計原則：
- ClassEnrollmentTarget 需配對 (year_end_cycle_id, semester_first, classroom_id)
- semester_first=True ↔ AppraisalCycle.semester == Semester.FIRST（上學期 Aug–Jan）
- 期末在籍 = retention.final_count（重用 _aggregate_class_retention 的已算結果，不重查）
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import (
    AUTO_ITEM_CODES,
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoringRule,
    CycleStatus,
    RoleGroup,
    ScoreItemCode,
    Semester,
)
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.employee import Employee, EmployeeType
from models.year_end import ClassEnrollmentTarget, YearEndCycle, YearEndCycleStatus
from services.appraisal.status_aggregator import aggregate_cycle_status

# ===== helpers =====


def _make_employee(session, name: str, eid_suffix: str = "HT1") -> Employee:
    emp = Employee(
        employee_id=f"E{eid_suffix}",
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_cycle(
    session, sy: int = 114, sem: Semester = Semester.FIRST
) -> AppraisalCycle:
    cycle = AppraisalCycle(
        academic_year=sy,
        semester=sem,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_classroom(session, name: str = "大班A") -> Classroom:
    cls = Classroom(name=name, school_year=114, semester=1, is_active=True)
    session.add(cls)
    session.flush()
    return cls


def _make_participant(session, cycle, employee, classroom_id=None):
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        classroom_id=classroom_id,
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return p


def _make_year_end_cycle(session, academic_year: int = 114) -> YearEndCycle:
    yec = YearEndCycle(
        academic_year=academic_year,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
        status=YearEndCycleStatus.OPEN,
        params_snapshot={},
    )
    session.add(yec)
    session.flush()
    return yec


def _make_enrollment_target(
    session,
    yec: YearEndCycle,
    classroom_id: int,
    head_count_target: int,
    semester_first: bool = True,
) -> ClassEnrollmentTarget:
    cet = ClassEnrollmentTarget(
        year_end_cycle_id=yec.id,
        semester_first=semester_first,
        classroom_id=classroom_id,
        head_count_target=head_count_target,
        avg_monthly_enrollment=Decimal("0"),
        class_performance_rate=Decimal("0"),
        returning_student_rate=Decimal("0"),
    )
    session.add(cet)
    session.flush()
    return cet


def _seed_active_students(session, classroom_id: int, count: int, prefix: str = "HS"):
    """Seed `count` lifecycle_active 期末在籍學生（enrollment_date 早於 cycle.start）。"""
    for i in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{i:04d}",
                name=f"學{prefix}{i}",
                classroom_id=classroom_id,
                enrollment_date=date(2025, 6, 1),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
        )
    session.flush()


# ===== tests =====


class TestHeadcountOverTarget:
    def test_headcount_over_target_computed(self, test_db_session):
        """期末在籍 14 人，編制 12 → headcount_over_target == 2。"""
        s = test_db_session
        emp = _make_employee(s, "陳老師", "HTC1")
        cls = _make_classroom(s, "中班甲")
        cycle = _make_cycle(s)  # Semester.FIRST → semester_first=True
        _make_participant(s, cycle, emp, classroom_id=cls.id)

        # 年終編制設定（同學年 academic_year=114，上學期）
        yec = _make_year_end_cycle(s, academic_year=114)
        _make_enrollment_target(
            s, yec, cls.id, head_count_target=12, semester_first=True
        )

        # 14 個期末 active 學生
        _seed_active_students(s, cls.id, 14, prefix="OT")
        s.commit()

        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].headcount_over_target == 2

    def test_headcount_under_target_zero(self, test_db_session):
        """期末在籍 17 人，編制 20 → headcount_over_target == 0（未超編不加分）。"""
        s = test_db_session
        emp = _make_employee(s, "林老師", "HTC2")
        cls = _make_classroom(s, "小班乙")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=cls.id)

        yec = _make_year_end_cycle(s, academic_year=114)
        _make_enrollment_target(
            s, yec, cls.id, head_count_target=20, semester_first=True
        )

        _seed_active_students(s, cls.id, 17, prefix="UT")
        s.commit()

        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].headcount_over_target == 0

    def test_no_year_end_target_fallback_zero(self, test_db_session):
        """無 YearEndCycle 或無 ClassEnrollmentTarget → headcount_over_target == 0（不崩潰）。"""
        s = test_db_session
        emp = _make_employee(s, "王老師", "HTC3")
        cls = _make_classroom(s, "大班丙")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=cls.id)

        # 故意不建 YearEndCycle 或 ClassEnrollmentTarget
        _seed_active_students(s, cls.id, 20, prefix="NT")
        s.commit()

        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].headcount_over_target == 0  # graceful fallback

    def test_no_classroom_participant_zero(self, test_db_session):
        """非帶班員工（classroom_id=None）→ headcount_over_target == 0。"""
        s = test_db_session
        emp = _make_employee(s, "行政人員", "HTC4")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=None)

        yec = _make_year_end_cycle(s, academic_year=114)
        s.commit()

        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].headcount_over_target == 0

    def test_semester_filter_second_semester(self, test_db_session):
        """下學期考核 (Semester.SECOND) 應對應 semester_first=False 的編制記錄。

        如果只有上學期記錄 (semester_first=True)，應 fallback 0（不混用）。
        """
        s = test_db_session
        emp = _make_employee(s, "張老師", "HTC5")
        cls = _make_classroom(s, "中班丁")
        # 下學期考核
        cycle = AppraisalCycle(
            academic_year=114,
            semester=Semester.SECOND,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
            base_score_calc_date=date(2026, 3, 15),
            base_score=Decimal("80.0"),
            status=CycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        _make_participant(s, cycle, emp, classroom_id=cls.id)

        yec = _make_year_end_cycle(s, academic_year=114)
        # 只建上學期記錄（semester_first=True）
        _make_enrollment_target(
            s, yec, cls.id, head_count_target=10, semester_first=True
        )

        _seed_active_students(s, cls.id, 15, prefix="SS")
        s.commit()

        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        # 下學期沒有對應記錄 → fallback 0（不應用上學期的編制）
        assert statuses[0].headcount_over_target == 0


class TestClassHeadcountAutoDelta:
    def test_class_headcount_in_auto_item_codes(self):
        """CLASS_HEADCOUNT_BONUS 必須在 AUTO_ITEM_CODES（從 MANUAL 移出）。"""
        assert ScoreItemCode.CLASS_HEADCOUNT_BONUS in AUTO_ITEM_CODES
        from models.appraisal import MANUAL_ITEM_CODES

        assert ScoreItemCode.CLASS_HEADCOUNT_BONUS not in MANUAL_ITEM_CODES

    def test_class_headcount_auto_delta_via_compute_all_deltas(
        self, test_db_session, monkeypatch
    ):
        """headcount_over_target=2 → CLASS_HEADCOUNT_BONUS delta == Decimal('4.0')。

        rule: PER_UNIT per_unit_delta=2 → 2 超編 × +2 = +4。
        """
        from services.appraisal import status_aggregator as agg
        from services.appraisal.status_aggregator import (
            ActivityRateAggregate,
            AttendanceAggregate,
            ClassRetentionAggregate,
            DisciplinaryAggregate,
            ParticipantStatus,
        )
        from services.appraisal.rule_applier import compute_all_deltas

        s = test_db_session
        cycle = AppraisalCycle(
            academic_year=114,
            semester=Semester.FIRST,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 1, 31),
            base_score_calc_date=date(2025, 9, 15),
            base_score=Decimal("75.6"),
            status=CycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        p = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=99,
            role_group=RoleGroup.HEAD_TEACHER,
            classroom_id=10,
            hire_months_in_cycle=Decimal("6"),
            is_excluded=False,
        )
        s.add(p)
        s.flush()

        # Seed only the CLASS_HEADCOUNT_BONUS rule (we only care about that delta)
        s.add(
            AppraisalScoringRule(
                item_code="CLASS_HEADCOUNT_BONUS",
                effective_from=date(2025, 1, 1),
                rule_type="PER_UNIT",
                rule_config={"per_unit_delta": 2},
            )
        )
        s.flush()

        fake_status = ParticipantStatus(
            participant_id=p.id,
            employee_id=99,
            employee_name="班導測試",
            role_group=RoleGroup.HEAD_TEACHER.value,
            classroom_id=10,
            attendance=AttendanceAggregate(employee_id=99),
            retention=ClassRetentionAggregate(
                employee_id=99,
                classroom_id=10,
                final_count=14,
            ),
            activity=ActivityRateAggregate(employee_id=99),
            disciplinary=DisciplinaryAggregate(employee_id=99),
            is_participant=True,
            hire_months_in_cycle=Decimal("6"),
            headcount_over_target=2,
        )
        monkeypatch.setattr(
            agg, "aggregate_cycle_status", lambda session, c: [fake_status]
        )
        s.commit()

        result = compute_all_deltas(s, cycle)

        dr = result[(p.id, "CLASS_HEADCOUNT_BONUS")]
        assert dr.delta == Decimal("4.00"), f"期望 4.00，實際 {dr.delta}"
        assert dr.raw_value == Decimal(
            "2"
        ), f"raw_value 應為超編人數 2，實際 {dr.raw_value}"
        assert "超編制" in dr.note, f"note 應含「超編制」，實際 {dr.note!r}"

    def test_class_headcount_zero_over_target_yields_zero_delta(
        self, test_db_session, monkeypatch
    ):
        """headcount_over_target=0 → delta=0（未超編不加分）。"""
        from services.appraisal import status_aggregator as agg
        from services.appraisal.status_aggregator import (
            ActivityRateAggregate,
            AttendanceAggregate,
            ClassRetentionAggregate,
            DisciplinaryAggregate,
            ParticipantStatus,
        )
        from services.appraisal.rule_applier import compute_all_deltas

        s = test_db_session
        cycle = AppraisalCycle(
            academic_year=114,
            semester=Semester.FIRST,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 1, 31),
            base_score_calc_date=date(2025, 9, 15),
            base_score=Decimal("75.6"),
            status=CycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        p = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=98,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=Decimal("6"),
            is_excluded=False,
        )
        s.add(p)
        s.flush()
        s.add(
            AppraisalScoringRule(
                item_code="CLASS_HEADCOUNT_BONUS",
                effective_from=date(2025, 1, 1),
                rule_type="PER_UNIT",
                rule_config={"per_unit_delta": 2},
            )
        )
        s.flush()

        fake_status = ParticipantStatus(
            participant_id=p.id,
            employee_id=98,
            employee_name="班導測試2",
            role_group=RoleGroup.HEAD_TEACHER.value,
            classroom_id=None,
            attendance=AttendanceAggregate(employee_id=98),
            retention=ClassRetentionAggregate(employee_id=98),
            activity=ActivityRateAggregate(employee_id=98),
            disciplinary=DisciplinaryAggregate(employee_id=98),
            is_participant=True,
            hire_months_in_cycle=Decimal("6"),
            headcount_over_target=0,
        )
        monkeypatch.setattr(
            agg, "aggregate_cycle_status", lambda session, c: [fake_status]
        )
        s.commit()

        result = compute_all_deltas(s, cycle)
        dr = result[(p.id, "CLASS_HEADCOUNT_BONUS")]
        assert dr.delta == Decimal("0.00")
