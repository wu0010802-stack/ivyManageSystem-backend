"""services/appraisal/status_aggregator.py 單元測試。

涵蓋四個 sub-aggregator（attendance / retention / activity / disciplinary）
+ facade `aggregate_cycle_status` 的邊界條件。使用 SQLite in-memory
DB（複用 conftest 的 `test_db_session` fixture）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.attendance import Attendance
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.disciplinary import (
    ACTION_TYPE_MAJOR,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_WARNING,
    DisciplinaryAction,
)
from models.activity import ActivityRegistration
from models.employee import Employee, EmployeeType
from services.appraisal.status_aggregator import aggregate_cycle_status


def _make_employee(session, name: str, eid_suffix: str = "001") -> Employee:
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


def _make_classroom(
    session, name: str = "大班A", sy: int = 114, sem: int = 1
) -> Classroom:
    cls = Classroom(name=name, school_year=sy, semester=sem, is_active=True)
    session.add(cls)
    session.flush()
    return cls


def _make_participant(
    session,
    cycle,
    employee,
    classroom_id=None,
    role_group=RoleGroup.HEAD_TEACHER,
    is_excluded=False,
):
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=employee.id,
        role_group=role_group,
        classroom_id=classroom_id,
        is_excluded=is_excluded,
    )
    session.add(p)
    session.flush()
    return p


class TestAttendanceAggregator:
    def test_attendance_aggregate_counts_late_early(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "王雅玲")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp)
        s.add_all(
            [
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 1),
                    is_late=True,
                ),
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 2),
                    is_late=True,
                ),
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 3),
                    is_early_leave=True,
                ),
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 4),
                    is_missing_punch_in=True,
                ),
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        att = statuses[0].attendance
        assert att.late_count == 2
        assert att.early_leave_count == 1
        assert att.missing_punch_count == 1

    def test_absent_day_not_double_counted_as_missing_punch(self, test_db_session):
        """同一天 status='absent'（曠職、無打卡）只應落 ABSENTEEISM 一條規則，
        不可同時被 MISSING_PUNCH ×2 重複計分（#11）。

        真實資料中曠職日由 sync_attendance_flags 把 is_missing_punch_in/out
        都設為 True（曠職日本就無打卡），若 missing_punch_count 把這兩個旗標
        算進去，該日會被 ABSENTEEISM（−4）與 MISSING_PUNCH（×2）雙重扣分。
        """
        s = test_db_session
        emp = _make_employee(s, "黃曠職")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp)
        s.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2025, 9, 10),
                status="absent",
                # 曠職日無打卡，旗標如 sync_attendance_flags 實際所設
                is_missing_punch_in=True,
                is_missing_punch_out=True,
            )
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        att = statuses[0].attendance
        assert att.absent_days == 1
        # 一日缺勤只落一條規則：曠職日不得再被 MISSING_PUNCH 重複計分
        assert att.missing_punch_count == 0

    def test_leave_day_not_counted_as_missing_punch(self, test_db_session):
        """status='leave'（全天請假）同樣不應被 MISSING_PUNCH 計分；
        防旁路寫入留下殘留旗標時的雙重計分（#11 防禦性收斂）。"""
        s = test_db_session
        emp = _make_employee(s, "李請假")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp)
        s.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2025, 9, 11),
                status="leave",
                # 模擬旁路寫入未清旗標的殘留狀態
                is_missing_punch_in=True,
                is_missing_punch_out=True,
            )
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        att = statuses[0].attendance
        assert att.leave_days == 1
        assert att.missing_punch_count == 0

    def test_real_missing_punch_still_counted_when_not_absent(self, test_db_session):
        """非曠職/請假日的真實缺卡仍應正常計入 MISSING_PUNCH（防過度排除）。"""
        s = test_db_session
        emp = _make_employee(s, "張缺卡")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp)
        s.add_all(
            [
                # 正常出勤日但漏打下班卡
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 12),
                    status="normal",
                    is_missing_punch_out=True,
                ),
                # 正常出勤日漏打上班卡
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 13),
                    status="normal",
                    is_missing_punch_in=True,
                ),
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        att = statuses[0].attendance
        assert att.missing_punch_count == 2
        assert att.absent_days == 0

    def test_attendance_excludes_dates_outside_cycle(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "蔡宜倩")
        cycle = _make_cycle(s)  # 2025-08-01 ~ 2026-01-31
        _make_participant(s, cycle, emp)
        s.add_all(
            [
                # cycle 前
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 7, 31),
                    is_late=True,
                ),
                # cycle 內
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2025, 9, 1),
                    is_late=True,
                ),
                # cycle 後（但今天在 cycle 結束前的話 end 會被 min(end, today) 改）
                # 為了測 cycle.end_date 之後不計，用 2099 確保也在 today 之後
                # 不會被計入，因為 end=min(cycle.end, today)
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        att = statuses[0].attendance
        assert att.late_count == 1  # cycle 前的不計


class TestClassRetentionAggregator:
    def test_class_retention_initial_vs_final(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "陳品棻")
        cls = _make_classroom(s)
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=cls.id)
        start = cycle.start_date  # 2025-08-01
        # 5 個學生開學前報到、其中 1 個中途退學
        students_data = [
            ("S001", "甲", date(2025, 6, 1), None, None, LIFECYCLE_ACTIVE),
            ("S002", "乙", date(2025, 6, 1), None, None, LIFECYCLE_ACTIVE),
            ("S003", "丙", date(2025, 6, 1), None, None, LIFECYCLE_ACTIVE),
            ("S004", "丁", date(2025, 6, 1), None, None, LIFECYCLE_ACTIVE),
            (
                "S005",
                "戊",
                date(2025, 6, 1),
                date(2025, 12, 1),
                None,
                LIFECYCLE_WITHDRAWN,
            ),
        ]
        for sid, name, enr, wd, grad, life in students_data:
            s.add(
                Student(
                    student_id=sid,
                    name=name,
                    classroom_id=cls.id,
                    enrollment_date=enr,
                    withdrawal_date=wd,
                    graduation_date=grad,
                    lifecycle_status=life,
                )
            )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        ret = statuses[0].retention
        assert ret.classroom_id == cls.id
        assert ret.initial_count == 5
        assert ret.final_count == 4  # 退學那位不計
        assert ret.retention_rate == Decimal("80.00")

    def test_class_retention_handles_no_classroom(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "潘諭慧")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=None)
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        ret = statuses[0].retention
        assert ret.classroom_id is None
        assert ret.initial_count == 0
        assert ret.final_count == 0
        assert ret.retention_rate == Decimal("0")

    def test_class_retention_zero_initial_returns_zero_no_div_by_zero(
        self, test_db_session
    ):
        s = test_db_session
        emp = _make_employee(s, "孔祥盈")
        cls = _make_classroom(s, name="新班")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp, classroom_id=cls.id)
        # 學生晚於 cycle.start_date 才入學 → initial=0
        s.add(
            Student(
                student_id="S100",
                name="阿明",
                classroom_id=cls.id,
                enrollment_date=date(2025, 12, 1),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        ret = statuses[0].retention
        assert ret.initial_count == 0
        assert ret.retention_rate == Decimal("0")


class TestActivityRateAggregator:
    def test_activity_rate_registration_basis(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "蔡佩汶")
        cls = _make_classroom(s)
        cycle = _make_cycle(s)  # sy=114, sem=FIRST → 對 ActivityRegistration sem=1
        _make_participant(s, cycle, emp, classroom_id=cls.id)
        # 5 個 active 學生
        student_ids = []
        for i in range(5):
            stu = Student(
                student_id=f"AS{i:03d}",
                name=f"學{i}",
                classroom_id=cls.id,
                enrollment_date=date(2025, 6, 1),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            s.add(stu)
            s.flush()
            student_ids.append(stu.id)
        # 2 位有 ActivityRegistration（is_active=True）
        for stu_id in student_ids[:2]:
            s.add(
                ActivityRegistration(
                    student_name="x",
                    classroom_id=cls.id,
                    school_year=114,
                    semester=1,
                    student_id=stu_id,
                    is_active=True,
                    paid_amount=0,
                )
            )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        act = statuses[0].activity
        assert act.classroom_id == cls.id
        assert act.enrolled_students == 5
        assert act.registered_for_activity == 2
        assert act.activity_rate == Decimal("40.00")


class TestDisciplinaryAggregator:
    def test_disciplinary_filters_by_action_date(self, test_db_session):
        s = test_db_session
        emp = _make_employee(s, "林姿妙")
        cycle = _make_cycle(s)
        _make_participant(s, cycle, emp)
        s.add_all(
            [
                DisciplinaryAction(
                    employee_id=emp.id,
                    action_date=date(2025, 9, 5),
                    action_type=ACTION_TYPE_WARNING,
                    deduction_amount=0,
                ),
                DisciplinaryAction(
                    employee_id=emp.id,
                    action_date=date(2025, 10, 5),
                    action_type=ACTION_TYPE_MINOR,
                    deduction_amount=0,
                ),
                # cycle 外
                DisciplinaryAction(
                    employee_id=emp.id,
                    action_date=date(2025, 7, 1),
                    action_type=ACTION_TYPE_MAJOR,
                    deduction_amount=0,
                ),
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        dis = statuses[0].disciplinary
        assert dis.warning_count == 1
        assert dis.minor_count == 1
        assert dis.major_count == 0  # cycle 外的不計
        assert len(dis.actions) == 2


class TestFacade:
    def test_aggregate_cycle_status_facade_returns_one_per_employee(
        self, test_db_session
    ):
        s = test_db_session
        cycle = _make_cycle(s)
        e1 = _make_employee(s, "A", eid_suffix="A01")
        e2 = _make_employee(s, "B", eid_suffix="B01")
        e3 = _make_employee(s, "C", eid_suffix="C01")
        _make_participant(s, cycle, e1)
        _make_participant(s, cycle, e2)
        _make_participant(s, cycle, e3)
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 3
        names = {st.employee_name for st in statuses}
        assert names == {"A", "B", "C"}

    def test_aggregate_cycle_status_skips_excluded_participants(self, test_db_session):
        s = test_db_session
        cycle = _make_cycle(s)
        e1 = _make_employee(s, "計算", eid_suffix="A01")
        e2 = _make_employee(s, "排除", eid_suffix="B01")
        _make_participant(s, cycle, e1, is_excluded=False)
        _make_participant(s, cycle, e2, is_excluded=True)
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        assert len(statuses) == 1
        assert statuses[0].employee_name == "計算"

    def test_aggregate_cycle_status_loads_manual_event_counts(self, test_db_session):
        """有 manual count 的 participant 應在 ParticipantStatus.manual_event_counts
        對應 item_code 看到正確 Decimal 數值；未填入者該 dict 為空。"""
        from models.appraisal import AppraisalManualEventCount, ScoreItemCode

        s = test_db_session
        cycle = _make_cycle(s)
        e1 = _make_employee(s, "有手填", eid_suffix="M01")
        e2 = _make_employee(s, "沒手填", eid_suffix="M02")
        p1 = _make_participant(s, cycle, e1)
        _make_participant(s, cycle, e2)
        s.flush()
        s.add_all(
            [
                AppraisalManualEventCount(
                    cycle_id=cycle.id,
                    participant_id=p1.id,
                    item_code=ScoreItemCode.SCHOOL_MEETING_ABSENCE.value,
                    count=Decimal("2"),
                ),
                AppraisalManualEventCount(
                    cycle_id=cycle.id,
                    participant_id=p1.id,
                    item_code=ScoreItemCode.CHILD_ACCIDENT.value,
                    count=Decimal("1"),
                ),
            ]
        )
        s.commit()
        statuses = aggregate_cycle_status(s, cycle)
        by_pid = {st.participant_id: st for st in statuses}
        assert p1.id in by_pid
        target = by_pid[p1.id]
        assert target.manual_event_counts["SCHOOL_MEETING_ABSENCE"] == Decimal("2")
        assert target.manual_event_counts["CHILD_ACCIDENT"] == Decimal("1")
        # 未手填者應為空 dict
        other = next(st for st in statuses if st.participant_id != p1.id)
        assert other.manual_event_counts == {}
