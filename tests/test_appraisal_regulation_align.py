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


# ===== Task 6-補：全校平均留校率去重共班 + 排除 initial=0 班 =====


class TestRetentionSchoolAvgDedup:
    def test_全校平均_排除initial為零的班且共班不雙計(self, test_db_session):
        """
        seed 情境：
          班A  — 10 個期初學生，全部留存（initial=10, final=10），
                兩位員工共帶（emp_a1, emp_a2，相同 classroom_id）。
          班B  — initial=0（期中新開），final=15（全數新生期末 active）。
          未帶班員工 emp_staff（classroom_id=None）。

        全校平均計算規則：
          - 班A 只計一次（去重），貢獻 initial=10, final=10。
          - 班B 排除（initial=0 的班不納入分母/分子）。
          - school_avg = 10/10 * 100 = 100.00%。
          - emp_staff.retention_rate == 100.00。
          - 帶班員工 emp_a1, emp_a2 的 retention_rate == 100.00（各自班級率）。
        """
        s = test_db_session
        cycle = _make_cycle_t46(s)

        # 班 A：10/10，兩位員工共帶
        cls_a = _make_classroom_t46(s, "大班DeupA")
        emp_a1 = _make_emp(s, "共班老師1", "TDUP1")
        emp_a2 = _make_emp(s, "共班老師2", "TDUP2")
        _make_participant_t46(s, cycle, emp_a1, classroom_id=cls_a.id)
        _make_participant_t46(s, cycle, emp_a2, classroom_id=cls_a.id)
        for i in range(10):
            s.add(
                Student(
                    student_id=f"TDUP_A{i:03d}",
                    name=f"A共{i}",
                    classroom_id=cls_a.id,
                    enrollment_date=date(2025, 6, 1),
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )

        # 班 B：initial=0（期中新開，期初時無學生），final=15（期末有 15 個 active）
        cls_b = _make_classroom_t46(s, "小班新開B")
        emp_b = _make_emp(s, "新開班老師", "TDUP3")
        _make_participant_t46(s, cycle, emp_b, classroom_id=cls_b.id)
        # 期末 15 個 active（全在 cycle 開始後才 enrollment_date，初期不算 initial）
        for i in range(15):
            s.add(
                Student(
                    student_id=f"TDUP_B{i:03d}",
                    name=f"B新{i}",
                    classroom_id=cls_b.id,
                    # enrollment_date > cycle.start_date(2025-08-01)，不算期初
                    enrollment_date=date(2025, 9, 1),
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )

        # 未帶班員工
        emp_staff = _make_emp(s, "未帶班主任", "TDUP4")
        _make_participant_t46(
            s, cycle, emp_staff, classroom_id=None, role_group=RoleGroup.STAFF
        )

        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        by_emp = {st.employee_id: st for st in statuses}

        # 班A 員工個人留校率 = 10/10 = 100.00
        assert by_emp[emp_a1.id].retention.retention_rate == Decimal("100.00")
        assert by_emp[emp_a2.id].retention.retention_rate == Decimal("100.00")
        # 班B 員工 initial=0 → 個人率 = 0（Decimal("0")）
        assert by_emp[emp_b.id].retention.retention_rate == Decimal("0")
        # 未帶班：全校平均 = 班A 10/10（班B 排除）= 100.00
        assert by_emp[emp_staff.id].retention.retention_rate == Decimal("100.00")


# ===== Task 7: 分年級門檻、獎懲加分側、merit action types =====


def test_flat_threshold_分年級門檻():
    from services.appraisal.rule_applier import apply_flat_threshold

    rule = ScoringRule(
        item_code="AFTER_CLASS_RATE",
        effective_from=date(2026, 2, 1),
        rule_type="FLAT_THRESHOLD",
        rule_config={
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
            "grade_thresholds": {"大班": 100, "中班": 90, "小班": 80, "幼幼班": 70},
        },
        applies_to_role_groups=None,
    )
    rg = RoleGroup.HEAD_TEACHER
    assert apply_flat_threshold(rule, Decimal("95"), rg, grade_name="大班") == Decimal(
        "0.00"
    )
    assert apply_flat_threshold(rule, Decimal("100"), rg, grade_name="大班") == Decimal(
        "2.00"
    )
    assert apply_flat_threshold(
        rule, Decimal("75"), rg, grade_name="幼幼班"
    ) == Decimal("2.00")
    assert apply_flat_threshold(rule, Decimal("85"), rg, grade_name=None) == Decimal(
        "2.00"
    )


def test_disciplinary_tiered_加分側():
    from services.appraisal.rule_applier import apply_disciplinary_tiered

    rule = ScoringRule(
        item_code="REWARD_PUNISH",
        effective_from=date(2026, 2, 1),
        rule_type="DISCIPLINARY_TIERED",
        rule_config={
            "warning_delta": -2.0,
            "minor_delta": -3.0,
            "major_delta": -6.0,
            "commend_delta": 2.0,
            "minor_merit_delta": 3.0,
            "major_merit_delta": 6.0,
        },
        applies_to_role_groups=None,
    )
    assert apply_disciplinary_tiered(
        rule,
        warning_count=1,
        minor_count=0,
        major_count=0,
        commend_count=0,
        minor_merit_count=0,
        major_merit_count=1,
    ) == Decimal("4.00")


def test_disciplinary_tiered_舊config向後相容():
    from services.appraisal.rule_applier import apply_disciplinary_tiered

    rule = ScoringRule(
        item_code="REWARD_PUNISH",
        effective_from=date(2025, 8, 1),
        rule_type="DISCIPLINARY_TIERED",
        rule_config={"warning_delta": -1.0, "minor_delta": -3.0, "major_delta": -10.0},
        applies_to_role_groups=None,
    )
    assert apply_disciplinary_tiered(rule, 1, 1, 0) == Decimal("-4.00")


def test_merit_action_types_註冊():
    from models.disciplinary import ACTION_TYPES, ACTION_TYPE_LABELS

    for t, label in (
        ("commendation", "嘉獎"),
        ("minor_merit", "小功"),
        ("major_merit", "大功"),
    ):
        assert t in ACTION_TYPES
        assert ACTION_TYPE_LABELS[t] == label


def test_status_out_schema_鏡像新欄位():
    """DisciplinaryAggregateOut 須鏡像 DisciplinaryAggregate 的三個 merit 計數。"""
    from schemas.appraisal import DisciplinaryAggregateOut

    out = DisciplinaryAggregateOut(
        warning_count=1,
        minor_count=0,
        major_count=0,
        commend_count=2,
        minor_merit_count=1,
        major_merit_count=0,
        suggested_score_delta=Decimal("0"),
    )
    assert out.commend_count == 2
    assert out.minor_merit_count == 1
    assert out.major_merit_count == 0


# ===== Task 8: compute_all_deltas 接線 =====


def _make_fake_status(
    *,
    participant_id: int = 1,
    employee_id: int = 1,
    absent_days: int = 0,
    reinstate_count: int = 0,
    activity_rate: Decimal = Decimal("0"),
    grade_name: str | None = None,
) -> "ParticipantStatus":
    """最小 fake status builder，供 Task 8 測試使用。"""
    from services.appraisal.status_aggregator import (
        ActivityRateAggregate,
        AttendanceAggregate,
        ClassRetentionAggregate,
        DisciplinaryAggregate,
        ParticipantStatus,
    )

    return ParticipantStatus(
        participant_id=participant_id,
        employee_id=employee_id,
        employee_name="測試員工",
        role_group=RoleGroup.HEAD_TEACHER.value,
        classroom_id=10,
        attendance=AttendanceAggregate(
            employee_id=employee_id,
            late_count=0,
            early_leave_count=0,
            missing_punch_count=0,
            leave_days=0,
            absent_days=absent_days,
        ),
        retention=ClassRetentionAggregate(
            employee_id=employee_id,
            retention_rate=Decimal("100"),
        ),
        activity=ActivityRateAggregate(
            employee_id=employee_id,
            activity_rate=activity_rate,
            grade_name=grade_name,
        ),
        disciplinary=DisciplinaryAggregate(
            employee_id=employee_id,
        ),
        is_participant=True,
        reinstate_count=reinstate_count,
    )


def test_auto_item_曠職與復學():
    """ABSENTEEISM / STUDENT_REINSTATE auto item 純函式驗證。"""
    from services.appraisal.rule_applier import ScoringRule, _apply_auto_item

    absenteeism_rule = ScoringRule(
        item_code="ABSENTEEISM",
        effective_from=date(2026, 2, 1),
        rule_type="PER_UNIT",
        rule_config={"per_unit_delta": -4},
        applies_to_role_groups=None,
    )
    reinstate_rule = ScoringRule(
        item_code="STUDENT_REINSTATE",
        effective_from=date(2026, 2, 1),
        rule_type="PER_UNIT",
        rule_config={"per_unit_delta": 1},
        applies_to_role_groups=None,
    )
    status = _make_fake_status(absent_days=2, reinstate_count=1)
    rg = RoleGroup.HEAD_TEACHER

    delta_abs, raw_abs, _ = _apply_auto_item(absenteeism_rule, status, rg)
    assert delta_abs == Decimal("-8.00")
    assert raw_abs == Decimal("2")

    delta_rei, raw_rei, _ = _apply_auto_item(reinstate_rule, status, rg)
    assert delta_rei == Decimal("1.00")
    assert raw_rei == Decimal("1")


def test_auto_item_才藝率帶年級門檻():
    """AFTER_CLASS_RATE auto item 帶 grade_name 走年級門檻。"""
    from services.appraisal.rule_applier import ScoringRule, _apply_auto_item

    rule = ScoringRule(
        item_code="AFTER_CLASS_RATE",
        effective_from=date(2026, 2, 1),
        rule_type="FLAT_THRESHOLD",
        rule_config={
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
            "grade_thresholds": {"大班": 100},
        },
        applies_to_role_groups=None,
    )
    # rate=95、大班門檻=100 → 未達門檻 → 0.00
    status = _make_fake_status(activity_rate=Decimal("95"), grade_name="大班")
    delta, raw, _ = _apply_auto_item(rule, status, RoleGroup.HEAD_TEACHER)
    assert delta == Decimal("0.00")
    assert raw == Decimal("95")


def test_compute_all_deltas_manual_delta(test_db_session, monkeypatch):
    """MANUAL_DELTA rule_type：手填分值 −3 在 clamp [−10, 0] 內，note 含「手填分值」。"""
    from models.appraisal import (
        AppraisalManualEventCount,
        AppraisalScoringRule,
        CycleStatus,
    )
    from services.appraisal import status_aggregator as agg
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
        employee_id=1,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()

    # 只 seed 兩條規則（auto + MANUAL_DELTA），其他 code 不在 rules → 只產出有規則的 item
    # 為讓測試簡單，只 seed CHILD_ACCIDENT（MANUAL_DELTA）+ LATE_EARLY（PER_UNIT）
    s.add(
        AppraisalScoringRule(
            item_code="CHILD_ACCIDENT",
            effective_from=date(2025, 8, 1),
            rule_type="MANUAL_DELTA",
            rule_config={"min_delta": -10, "max_delta": 0},
        )
    )
    s.add(
        AppraisalScoringRule(
            item_code="LATE_EARLY",
            effective_from=date(2025, 8, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.25},
        )
    )
    # 手填 CHILD_ACCIDENT count = −3
    s.add(
        AppraisalManualEventCount(
            cycle_id=cycle.id,
            participant_id=p.id,
            item_code="CHILD_ACCIDENT",
            count=Decimal("-3"),
        )
    )
    s.flush()

    fake_status = _make_fake_status(participant_id=p.id, employee_id=1)
    monkeypatch.setattr(agg, "aggregate_cycle_status", lambda session, c: [fake_status])

    result = compute_all_deltas(s, cycle)

    child_acc = result[(p.id, "CHILD_ACCIDENT")]
    assert child_acc.delta == Decimal("-3.00")
    assert "手填分值" in child_acc.note


# ===== code-review 修正：merit 類型永不產生薪資扣款 =====


def test_merit列不產生薪資扣款_commendation():
    """嘉獎 HR 誤填金額 → _effective_amount 回 0。"""
    from services.disciplinary import _effective_amount

    class _A:
        action_type = "commendation"
        deduction_amount = 500  # HR 誤填

    assert _effective_amount(_A(), bonus_config=None) == 0.0


def test_merit列不產生薪資扣款_minor_merit():
    """小功 HR 誤填金額 → _effective_amount 回 0。"""
    from services.disciplinary import _effective_amount

    class _A:
        action_type = "minor_merit"
        deduction_amount = 3000

    assert _effective_amount(_A(), bonus_config=None) == 0.0


def test_merit列不產生薪資扣款_major_merit():
    """大功 HR 誤填金額 → _effective_amount 回 0。"""
    from services.disciplinary import _effective_amount

    class _A:
        action_type = "major_merit"
        deduction_amount = 9999

    assert _effective_amount(_A(), bonus_config=None) == 0.0


def test_懲處類型仍正常產生扣款():
    """警告/小過 不受 merit 守衛影響，仍回傳正確金額。"""
    from services.disciplinary import _effective_amount

    class _Warning:
        action_type = "warning"
        deduction_amount = 1000

    class _Minor:
        action_type = "minor"
        deduction_amount = 0  # fallback

    assert _effective_amount(_Warning(), bonus_config=None) == 1000.0
    # deduction_amount=0 → resolve_default_amount fallback=3000
    assert _effective_amount(_Minor(), bonus_config=None) == 3000.0


def test_scoring_rule_in_接受manual_delta():
    from schemas.appraisal import ScoringRuleIn

    r = ScoringRuleIn(
        item_code="CHILD_ACCIDENT",
        effective_from="2026-02-01",
        rule_type="MANUAL_DELTA",
        rule_config={"min_delta": -10, "max_delta": 0},
        applies_to_role_groups=None,
    )
    assert r.rule_type == "MANUAL_DELTA"


# ===== Task 10: manual_event_counts API 的 MANUAL_DELTA 分值範圍驗證 =====

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.appraisal import (
    AppraisalScoringRule,
)
from models.auth import User
from models.database import Base
from models.employee import Employee
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db_t10(tmp_path):
    """Task 10 專用：SQLite + TestClient，含 appraisal_router。"""
    db_path = tmp_path / "appraisal-t10.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_t10(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_t10_cycle_rule_participant(session):
    """建立 cycle（base_score_calc_date >= 2026-02-01）+ CHILD_ACCIDENT MANUAL_DELTA 規則 + participant。"""
    emp = Employee(
        employee_id="E010",
        name="測試員工",
        is_active=True,
    )
    session.add(emp)
    session.flush()

    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.SECOND,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
        base_score_calc_date=date(2026, 3, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()

    session.add(
        AppraisalScoringRule(
            item_code="CHILD_ACCIDENT",
            effective_from=date(2026, 2, 1),
            rule_type="MANUAL_DELTA",
            rule_config={"min_delta": -10, "max_delta": 0},
        )
    )
    session.flush()

    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return cycle.id, p.id


class TestManualEventCountsManualDeltaValidation:
    def test_manual_delta_超出範圍_422_含detail範圍(self, client_with_db_t10):
        """count=-15 超出 [-10, 0] → 422，detail 含「範圍」。"""
        client, sf = client_with_db_t10
        with sf() as s:
            user = User(
                username="admin_t10",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["APPRAISAL_EVENT_WRITE", "APPRAISAL_READ"],
                is_active=True,
            )
            s.add(user)
            s.flush()
            cycle_id, p_id = _setup_t10_cycle_rule_participant(s)
            s.commit()
        assert _login_t10(client, "admin_t10").status_code == 200

        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "CHILD_ACCIDENT",
                        "count": "-15",
                    }
                ]
            },
        )
        assert r.status_code == 422, r.text
        assert "範圍" in r.json().get("detail", ""), r.text

    def test_manual_delta_範圍內_成功(self, client_with_db_t10):
        """count=-3 在 [-10, 0] 內 → 200。"""
        client, sf = client_with_db_t10
        with sf() as s:
            user = User(
                username="admin_t10b",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["APPRAISAL_EVENT_WRITE", "APPRAISAL_READ"],
                is_active=True,
            )
            s.add(user)
            s.flush()
            cycle_id, p_id = _setup_t10_cycle_rule_participant(s)
            s.commit()
        assert _login_t10(client, "admin_t10b").status_code == 200

        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "CHILD_ACCIDENT",
                        "count": "-3",
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text


# ===== Task 11: 歷史保護 + effective 邊界測試 =====


def test_effective_邊界_0131用舊版_0201用新版(test_db_session):
    from datetime import date as d

    from models.appraisal import AppraisalScoringRule
    from services.appraisal.rule_applier import load_rules_for_date

    test_db_session.add_all(
        [
            AppraisalScoringRule(
                item_code="RETURNING_RATE_0315",
                effective_from=d(2025, 8, 1),
                rule_type="TIER",
                rule_config={"tiers": [{"min": 0, "delta": -6.0}]},
                applies_to_role_groups=["HEAD_TEACHER", "ASSISTANT"],
            ),
            AppraisalScoringRule(
                item_code="RETURNING_RATE_0315",
                effective_from=d(2026, 2, 1),
                rule_type="TIER",
                rule_config={"tiers": [{"min": 0, "delta": -4.0}]},
                applies_to_role_groups=None,
            ),
        ]
    )
    test_db_session.flush()
    old = load_rules_for_date(test_db_session, d(2026, 1, 31))["RETURNING_RATE_0315"]
    new = load_rules_for_date(test_db_session, d(2026, 2, 1))["RETURNING_RATE_0315"]
    assert old.effective_from == d(2025, 8, 1)
    assert new.effective_from == d(2026, 2, 1)
    assert new.applies_to_role_groups is None


# ===== Task 12: 規章值金標準測試 =====


def test_規章金標準_主管優等():
    """base 160/160=100、deltas [-8] → total 92 優等；rate 10000 → bonus 9200.00"""
    from datetime import date as d

    from services.appraisal.engine import (
        BonusRateLookup,
        Grade,
        compute_summary,
    )

    rates = BonusRateLookup(
        rates={
            ("2026-02-01", RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("10000"),
        }
    )
    result = compute_summary(
        actual_enrollment=160,
        enrollment_target=160,
        score_deltas=[Decimal("-8")],
        role_group=RoleGroup.SUPERVISOR,
        bonus_rates=rates,
        on_date=d(2026, 3, 15),
    )
    assert result.base_score == Decimal("100.0")
    assert result.total_score == Decimal("92.00")
    assert result.grade == Grade.OUTSTANDING
    assert result.bonus_amount == Decimal("9200.00")


def test_規章金標準_廚師甲等3500():
    """base 121/160=75.6、deltas [+6,+2] → total 83.6 甲等；COOK rate 3500 → 2926.00"""
    from datetime import date as d

    from services.appraisal.engine import (
        BonusRateLookup,
        Grade,
        compute_summary,
    )

    rates = BonusRateLookup(
        rates={
            ("2026-02-01", RoleGroup.COOK, Grade.GOOD): Decimal("3500"),
        }
    )
    result = compute_summary(
        actual_enrollment=121,
        enrollment_target=160,
        score_deltas=[Decimal("6"), Decimal("2")],
        role_group=RoleGroup.COOK,
        bonus_rates=rates,
        on_date=d(2026, 3, 15),
    )
    assert result.base_score == Decimal("75.6")
    assert result.total_score == Decimal("83.60")
    assert result.grade == Grade.GOOD
    assert result.bonus_amount == Decimal("2926.00")
