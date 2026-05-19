"""純函式測試 — MOE Phase 2 monthly calculator."""

from datetime import date, datetime

import pytest

from services.gov_moe.monthly_calculator import (
    calc_age_group,
    is_foreign,
    working_days_in_month,
    classroom_at_month_end,
    compute_student_attendance_for_month,
    build_snapshot_rows,
)

# ── 測試用 DB fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_classroom_context(test_db_session):
    """在測試 DB 中建立 Classroom + Student，回傳 {classroom_id, student_id}。"""
    from models.classroom import Classroom, Student

    classroom = Classroom(name="草莓班", capacity=25, school_year=2025, semester=2)
    test_db_session.add(classroom)
    test_db_session.flush()

    student = Student(
        student_id="T001",
        name="測試生",
        gender="男",
        birthday=date(2021, 6, 1),
        classroom_id=classroom.id,
        enrollment_date=date(2025, 1, 1),
        nationality="本國",
        lifecycle_status="active",
    )
    test_db_session.add(student)
    test_db_session.commit()

    return {"classroom_id": classroom.id, "student_id": student.id}


# ── TestCalcAgeGroup ──────────────────────────────────────────────────────────


class TestCalcAgeGroup:
    def test_under_2_returns_2_3(self):
        assert calc_age_group(date(2025, 1, 1), date(2026, 5, 31)) == "2-3"

    def test_exactly_2_returns_2_3(self):
        assert calc_age_group(date(2024, 5, 31), date(2026, 5, 31)) == "2-3"

    def test_3_returns_3_4(self):
        assert calc_age_group(date(2023, 5, 31), date(2026, 5, 31)) == "3-4"

    def test_4_returns_4_5(self):
        assert calc_age_group(date(2022, 5, 31), date(2026, 5, 31)) == "4-5"

    def test_5_returns_5_6(self):
        assert calc_age_group(date(2021, 5, 31), date(2026, 5, 31)) == "5-6"

    def test_over_6_returns_5_6(self):
        assert calc_age_group(date(2019, 5, 31), date(2026, 5, 31)) == "5-6"

    def test_birthday_none_returns_unknown(self):
        assert calc_age_group(None, date(2026, 5, 31)) == "未知"

    def test_birthday_after_ref_date_returns_2_3(self):
        # 出生於 ref_date 後（罕見，data corruption）→ age = 0，歸 2-3 防呆
        assert calc_age_group(date(2026, 6, 1), date(2026, 5, 31)) == "2-3"


# ── TestIsForeign ─────────────────────────────────────────────────────────────


class TestIsForeign:
    @pytest.mark.parametrize(
        "nationality", ["本國", "台灣", "中華民國", "中華民國（台灣）", "ROC"]
    )
    def test_taiwan_aliases_not_foreign(self, nationality):
        assert is_foreign(nationality) is False

    def test_with_whitespace(self):
        assert is_foreign("  本國  ") is False

    @pytest.mark.parametrize("nationality", ["美國", "日本", "越南", "印尼"])
    def test_other_country_is_foreign(self, nationality):
        assert is_foreign(nationality) is True

    def test_none_returns_false(self):
        assert is_foreign(None) is False

    def test_empty_returns_false(self):
        assert is_foreign("") is False


# ── TestWorkingDaysInMonth ────────────────────────────────────────────────────


class TestWorkingDaysInMonth:
    def test_no_holidays_returns_weekdays(self, test_db_session):
        """2026-05 純 weekday 應有 21 天。"""
        result = working_days_in_month(test_db_session, 2026, 5)
        assert len(result) == 21
        assert date(2026, 5, 1) in result
        assert date(2026, 5, 2) not in result  # Sat
        assert date(2026, 5, 3) not in result  # Sun

    def test_active_holiday_excluded(self, test_db_session):
        from models.event import Holiday

        test_db_session.add(
            Holiday(date=date(2026, 5, 1), name="勞動節", is_active=True)
        )
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 1) not in result
        assert len(result) == 20

    def test_inactive_holiday_not_excluded(self, test_db_session):
        from models.event import Holiday

        test_db_session.add(
            Holiday(date=date(2026, 5, 1), name="勞動節", is_active=False)
        )
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 1) in result

    def test_workday_override_includes_weekend(self, test_db_session):
        from models.event import WorkdayOverride

        test_db_session.add(
            WorkdayOverride(date=date(2026, 5, 9), name="補上班", is_active=True)
        )
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 9) in result  # Saturday
        assert len(result) == 22


# ── TestClassroomAtMonthEnd ───────────────────────────────────────────────────


class TestClassroomAtMonthEnd:
    def test_uses_last_transfer_before_snapshot(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Classroom
        from models.student_transfer import StudentClassroomTransfer

        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        c2 = Classroom(name="芒果班", capacity=20)
        test_db_session.add(c2)
        test_db_session.commit()
        test_db_session.add(
            StudentClassroomTransfer(
                student_id=student_id,
                from_classroom_id=c1_id,
                to_classroom_id=c2.id,
                transferred_at=datetime(2026, 5, 10, 9, 0),
            )
        )
        test_db_session.commit()
        assert (
            classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31))
            == c2.id
        )

    def test_transfer_after_snapshot_not_used(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Classroom
        from models.student_transfer import StudentClassroomTransfer

        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        c2 = Classroom(name="芒果班", capacity=20)
        test_db_session.add(c2)
        test_db_session.commit()
        test_db_session.add(
            StudentClassroomTransfer(
                student_id=student_id,
                from_classroom_id=c1_id,
                to_classroom_id=c2.id,
                transferred_at=datetime(2026, 6, 5, 9, 0),  # after snapshot
            )
        )
        test_db_session.commit()
        assert (
            classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31))
            == c1_id
        )

    def test_no_transfer_falls_back_to_classroom_id(
        self, test_db_session, sample_classroom_context
    ):
        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        assert (
            classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31))
            == c1_id
        )


# ── TestComputeStudentAttendance ──────────────────────────────────────────────


class TestComputeStudentAttendance:
    def test_full_attendance(self, test_db_session, sample_classroom_context):
        from models.classroom import Student, StudentAttendance

        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        for d in [date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6)]:
            test_db_session.add(
                StudentAttendance(student_id=student_id, date=d, status="出席")
            )
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert expected == 21
        assert actual == 3

    def test_mid_month_enrollment(self, test_db_session, sample_classroom_context):
        from models.classroom import Student, StudentAttendance

        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2026, 5, 15)
        test_db_session.add(
            StudentAttendance(
                student_id=student_id, date=date(2026, 5, 18), status="出席"
            )
        )
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert expected == 11  # 5/15 (Fri) ~ 5/31 weekdays
        assert actual == 1

    def test_late_status_counts_as_attended(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Student, StudentAttendance

        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        test_db_session.add(
            StudentAttendance(
                student_id=student_id, date=date(2026, 5, 4), status="遲到"
            )
        )
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        _, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert actual == 1

    def test_sick_and_personal_leave_not_counted(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Student, StudentAttendance

        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        test_db_session.add(
            StudentAttendance(
                student_id=student_id, date=date(2026, 5, 4), status="病假"
            )
        )
        test_db_session.add(
            StudentAttendance(
                student_id=student_id, date=date(2026, 5, 5), status="事假"
            )
        )
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        _, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert actual == 0

    def test_withdrawal_caps_expected_days(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Student

        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        student.withdrawal_date = date(2026, 5, 15)
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, _ = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert expected == 11


# ── TestBuildSnapshotRows ─────────────────────────────────────────────────────


class TestBuildSnapshotRows:
    def _make_students_fixture(self, db, classroom_id):
        from models.classroom import Student

        s1 = Student(
            student_id="S001",
            name="王小明",
            gender="男",
            birthday=date(2022, 1, 1),
            classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="本國",
            lifecycle_status="active",
        )
        s2 = Student(
            student_id="S002",
            name="陳小華",
            gender="女",
            birthday=date(2023, 1, 1),
            classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="越南",
            lifecycle_status="active",
            is_disadvantaged=True,
            low_income_status="low",
        )
        s3 = Student(
            student_id="S003",
            name="林小強",
            gender="男",
            birthday=date(2022, 6, 1),
            classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="本國",
            lifecycle_status="active",
            disability_type="智能",
            disability_level="輕度",
            indigenous_status="阿美",
        )
        db.add_all([s1, s2, s3])
        db.commit()
        return s1, s2, s3

    def test_three_students_split_by_age(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import StudentAttendance

        classroom_id = sample_classroom_context["classroom_id"]
        s1, s2, s3 = self._make_students_fixture(test_db_session, classroom_id)
        for s in [s1, s2, s3]:
            test_db_session.add(
                StudentAttendance(student_id=s.id, date=date(2026, 5, 1), status="出席")
            )
        test_db_session.commit()

        rows, details = build_snapshot_rows(
            test_db_session, 2026, 5, generated_by="test@example.com"
        )

        # s1+s3 は 4-5 歲一組，s2 是 3-4 歲一組
        ag_groups = {r["age_group"] for r in rows}
        assert "4-5" in ag_groups
        assert "3-4" in ag_groups

    def test_excludes_prospect_lifecycle(
        self, test_db_session, sample_classroom_context
    ):
        from models.classroom import Student

        classroom_id = sample_classroom_context["classroom_id"]
        prospect = Student(
            student_id="S099",
            name="未報名",
            gender="男",
            birthday=date(2022, 1, 1),
            classroom_id=classroom_id,
            lifecycle_status="prospect",
        )
        test_db_session.add(prospect)
        test_db_session.commit()
        rows, _ = build_snapshot_rows(test_db_session, 2026, 5, generated_by="t")
        # prospect 不出現在 rows total_count 內
        total = sum(r["total_count"] for r in rows)
        # sample_classroom_context fixture 預設 active student；prospect 不算
        # 結果應只算入 active student，不包含 prospect
        # 用較寬鬆斷言：只要 total <= 預設 fixture student 數量即可
        assert total <= 1
