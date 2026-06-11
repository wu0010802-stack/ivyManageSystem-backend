"""tests/test_appraisal_regulation_align.py — 規章第六篇對齊（spec 2026-06-11）"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import (
    AUTO_ITEM_CODES,
    MANUAL_ITEM_CODES,
    AppraisalCycle,
    AppraisalParticipant,
    CycleStatus,
    RoleGroup,
    ScoreItemCode,
    Semester,
)
from models.attendance import Attendance
from models.classroom import (
    LIFECYCLE_ACTIVE,
    ClassGrade,
    Classroom,
    Student,
)
from models.employee import Employee, EmployeeType
from models.student_log import StudentChangeLog
from services.appraisal.rule_applier import ScoringRule, apply_manual_delta
from services.appraisal.status_aggregator import aggregate_cycle_status

NEW_CODES = {
    "ABSENTEEISM",
    "STUDENT_WITHDRAWAL",
    "STUDENT_REINSTATE",
    "TRIAL_LEAVE",
    "CLASS_TRANSFER",
    "EXAM_RESULT",
    "RECRUIT_SCORE",
    "SUPERVISOR_SCORE",
    "EXCELLENCE_NOMINATION",
}


def test_score_item_code_新增九項():
    assert NEW_CODES <= {c.value for c in ScoreItemCode}


def test_auto_manual_歸類():
    assert ScoreItemCode.ABSENTEEISM in AUTO_ITEM_CODES
    assert ScoreItemCode.STUDENT_REINSTATE in AUTO_ITEM_CODES
    # 休學降級手填（spec §14.3）；其餘新項皆手填
    for code in (
        "STUDENT_WITHDRAWAL",
        "TRIAL_LEAVE",
        "CLASS_TRANSFER",
        "EXAM_RESULT",
        "RECRUIT_SCORE",
        "SUPERVISOR_SCORE",
        "EXCELLENCE_NOMINATION",
    ):
        assert ScoreItemCode(code) in MANUAL_ITEM_CODES


# ===== Task 3: MANUAL_DELTA 規則型別 =====


def _md_rule(lo, hi):
    return ScoringRule(
        item_code="CHILD_ACCIDENT",
        effective_from=date(2026, 2, 1),
        rule_type="MANUAL_DELTA",
        rule_config={"min_delta": lo, "max_delta": hi},
        applies_to_role_groups=None,
    )


def test_manual_delta_範圍內原值():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-3.5"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-3.50"
    )


def test_manual_delta_下限clamp():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-15"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-10.00"
    )


def test_manual_delta_上限clamp():
    rule = _md_rule(0, 20)
    assert apply_manual_delta(rule, Decimal("25"), RoleGroup.HEAD_TEACHER) == Decimal(
        "20.00"
    )


def test_manual_delta_邊界值不截斷():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-10"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-10.00"
    )
    assert apply_manual_delta(rule, Decimal("0"), RoleGroup.HEAD_TEACHER) == Decimal(
        "0.00"
    )


# ===== Helpers shared by Task 4-6 tests =====


def _make_emp(session, name, eid_suffix):
    emp = Employee(
        employee_id=f"E{eid_suffix}",
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_cycle_t46(session):
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_classroom_t46(session, name="大班A", grade_name=None):
    grade = None
    if grade_name:
        grade = ClassGrade(name=grade_name, sort_order=1)
        session.add(grade)
        session.flush()
    cls = Classroom(
        name=name,
        school_year=114,
        semester=1,
        is_active=True,
        grade_id=grade.id if grade else None,
    )
    session.add(cls)
    session.flush()
    return cls


def _make_participant_t46(
    session, cycle, emp, classroom_id=None, role_group=RoleGroup.HEAD_TEACHER
):
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=role_group,
        classroom_id=classroom_id,
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return p


# ===== Task 4: 考勤聚合 leave/absent 分流 =====


class TestAttendanceLeaveAbsentSplit:
    def test_attendance_aggregate_leave與absent分流(self, test_db_session):
        """status='leave' 算請假、status='absent' 算曠職，不再混用。"""
        s = test_db_session
        emp = _make_emp(s, "林小明", "T4A")
        cycle = _make_cycle_t46(s)
        _make_participant_t46(s, cycle, emp)
        # 2 筆 status='leave'（全天請假）、1 筆 status='absent'（曠職）
        s.add_all(
            [
                Attendance(
                    employee_id=emp.id, attendance_date=date(2025, 9, 1), status="leave"
                ),
                Attendance(
                    employee_id=emp.id, attendance_date=date(2025, 9, 2), status="leave"
                ),
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 3),
                    status="absent",
                ),
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        att = statuses[0].attendance
        assert att.leave_days == 2  # status='leave' 才算請假
        assert att.absent_days == 1  # status='absent' 才算曠職


# ===== Task 5: 復學事件自動計數 =====


class TestReinstateCount:
    def test_reinstate_count_有復學事件(self, test_db_session):
        """StudentChangeLog event_type='復學' 落在 cycle 窗內且同班，計入 reinstate_count。"""
        s = test_db_session
        emp = _make_emp(s, "陳老師", "T5A")
        cycle = _make_cycle_t46(s)
        cls = _make_classroom_t46(s, "中班A")
        _make_participant_t46(s, cycle, emp, classroom_id=cls.id)
        # seed 1 筆復學事件，event_date 在 cycle 窗內，classroom_id 同班
        stu = Student(
            student_id="SCL01",
            name="復學小明",
            classroom_id=cls.id,
            enrollment_date=date(2025, 6, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s.add(stu)
        s.flush()
        s.add(
            StudentChangeLog(
                student_id=stu.id,
                school_year=114,
                semester=1,
                event_type="復學",
                event_date=date(2025, 10, 1),
                classroom_id=cls.id,
            )
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].reinstate_count == 1

    def test_reinstate_count_無班級者為零(self, test_db_session):
        """未帶班 participant（classroom_id=None）的 reinstate_count 為 0。"""
        s = test_db_session
        emp = _make_emp(s, "主任老師", "T5B")
        cycle = _make_cycle_t46(s)
        _make_participant_t46(s, cycle, emp, classroom_id=None)
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].reinstate_count == 0


# ===== Task 6: 未帶班全校平均留校率 + 才藝聚合年級名 =====


class TestRetentionNoBand:
    def test_無班級者留校率為全校加權平均(self, test_db_session):
        """未帶班 participant 的 retention_rate == 全校加權平均（2 位 HALF_UP）。

        兩個班：10/10=100%、5/10=50% → 全校 (10+5)/(10+10)*100 = 75.00。
        """
        s = test_db_session
        cycle = _make_cycle_t46(s)

        # 班 A：10/10 = 100%（10 個期初學生，全留）
        cls_a = _make_classroom_t46(s, "大班A")
        emp_a = _make_emp(s, "老師A", "T6A1")
        _make_participant_t46(s, cycle, emp_a, classroom_id=cls_a.id)
        for i in range(10):
            s.add(
                Student(
                    student_id=f"T6A{i:03d}",
                    name=f"A學{i}",
                    classroom_id=cls_a.id,
                    enrollment_date=date(2025, 6, 1),
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )

        # 班 B：5/10 = 50%（10 個期初，5 個留到期末 active）
        cls_b = _make_classroom_t46(s, "中班B")
        emp_b = _make_emp(s, "老師B", "T6B1")
        _make_participant_t46(s, cycle, emp_b, classroom_id=cls_b.id)
        from models.classroom import LIFECYCLE_WITHDRAWN

        for i in range(5):
            s.add(
                Student(
                    student_id=f"T6B{i:03d}",
                    name=f"B學{i}",
                    classroom_id=cls_b.id,
                    enrollment_date=date(2025, 6, 1),
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        for i in range(5, 10):
            s.add(
                Student(
                    student_id=f"T6B{i:03d}",
                    name=f"B學{i}",
                    classroom_id=cls_b.id,
                    enrollment_date=date(2025, 6, 1),
                    withdrawal_date=date(2025, 12, 1),
                    lifecycle_status=LIFECYCLE_WITHDRAWN,
                )
            )

        # 未帶班員工（STAFF，classroom_id=None）
        emp_staff = _make_emp(s, "主任", "T6S1")
        _make_participant_t46(
            s, cycle, emp_staff, classroom_id=None, role_group=RoleGroup.STAFF
        )

        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        staff_st = next(st for st in statuses if st.employee_id == emp_staff.id)
        # 全校加權：Σfinal/Σinitial = (10+5)/(10+10) = 75.00%
        assert staff_st.retention.retention_rate == Decimal("75.00")


def test_status_out_schema_鏡像新欄位():
    from schemas.appraisal import (
        ActivityRateAggregateOut,
        AttendanceAggregateOut,
        ParticipantStatusOut,
    )

    assert "absent_days" in AttendanceAggregateOut.model_fields
    assert "reinstate_count" in ParticipantStatusOut.model_fields
    assert "grade_name" in ActivityRateAggregateOut.model_fields


class TestActivityGradeName:
    def test_才藝聚合帶年級名(self, test_db_session):
        """ActivityRateAggregate.grade_name 對應 Classroom.grade.name。"""
        s = test_db_session
        emp = _make_emp(s, "才藝老師", "T6G1")
        cycle = _make_cycle_t46(s)
        cls = _make_classroom_t46(s, "大班C", grade_name="大班")
        _make_participant_t46(s, cycle, emp, classroom_id=cls.id)
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].activity.grade_name == "大班"
