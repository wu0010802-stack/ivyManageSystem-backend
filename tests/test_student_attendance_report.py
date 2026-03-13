from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.auth import User
from models.classroom import Classroom, Student, StudentAttendance
from models.event import Holiday
from services.student_attendance_report import build_daily_classroom_overview, build_monthly_attendance_report


class TestStudentAttendanceReport:

    def setup_method(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)

    def test_monthly_report_excludes_holidays_and_flags_absence_streak(self):
        session = self.Session()
        try:
            classroom = Classroom(name="向日葵班", is_active=True)
            session.add(classroom)
            session.flush()

            student_a = Student(
                student_id="S001",
                name="王小明",
                classroom_id=classroom.id,
                is_active=True,
                enrollment_date=date(2026, 1, 1),
            )
            student_b = Student(
                student_id="S002",
                name="陳小花",
                classroom_id=classroom.id,
                is_active=True,
                enrollment_date=date(2026, 1, 8),
            )
            session.add_all([student_a, student_b])
            session.add(Holiday(date=date(2026, 1, 1), name="元旦", is_active=True))
            session.flush()

            session.add_all([
                StudentAttendance(student_id=student_a.id, date=date(2026, 1, 5), status="出席"),
                StudentAttendance(student_id=student_a.id, date=date(2026, 1, 6), status="缺席"),
                StudentAttendance(student_id=student_a.id, date=date(2026, 1, 7), status="缺席"),
                StudentAttendance(student_id=student_a.id, date=date(2026, 1, 8), status="缺席"),
                StudentAttendance(student_id=student_a.id, date=date(2026, 1, 9), status="遲到"),
                StudentAttendance(student_id=student_b.id, date=date(2026, 1, 8), status="出席"),
                StudentAttendance(student_id=student_b.id, date=date(2026, 1, 9), status="出席"),
            ])
            session.commit()

            report = build_monthly_attendance_report(session, classroom.id, 2026, 1)

            assert report["school_days_count"] == 21
            assert report["holiday_count"] == 1
            assert report["classroom_name"] == "向日葵班"
            assert len(report["alerts"]) == 1

            student_a_report = next(item for item in report["students"] if item["student_no"] == "S001")
            assert student_a_report["school_days"] == 21
            assert student_a_report["出席"] == 1
            assert student_a_report["遲到"] == 1
            assert student_a_report["缺席"] == 3
            assert student_a_report["未點名"] == 16
            assert student_a_report["attendance_rate"] == 9.5
            assert student_a_report["longest_absence_streak"] == 3
            assert student_a_report["absence_alert"] is True

            student_b_report = next(item for item in report["students"] if item["student_no"] == "S002")
            assert student_b_report["school_days"] == 17
            assert student_b_report["出席"] == 2
            assert student_b_report["未點名"] == 15
            assert student_b_report["attendance_rate"] == 11.8

            assert report["classroom_attendance_rate"] == 10.5
            assert report["classroom_record_completion_rate"] == 18.4
        finally:
            session.close()


class TestDailyClassroomOverview:

    def setup_method(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)

    def test_overview_aggregates_classroom_counts_and_rollcall_status(self):
        session = self.Session()
        try:
            sun = Classroom(name="向日葵班", is_active=True)
            moon = Classroom(name="月亮班", is_active=True)
            star = Classroom(name="星星班", is_active=True)
            session.add_all([sun, moon, star])
            session.flush()

            user = User(username="admin_rollcall", password_hash="hashed", role="admin", is_active=True)
            session.add(user)
            session.flush()

            students = [
                Student(student_id="S001", name="小明", classroom_id=sun.id, is_active=True),
                Student(student_id="S002", name="小美", classroom_id=sun.id, is_active=True),
                Student(student_id="M001", name="小月", classroom_id=moon.id, is_active=True),
                Student(student_id="X001", name="小星", classroom_id=star.id, is_active=True),
                Student(student_id="X002", name="小晴", classroom_id=star.id, is_active=True),
            ]
            session.add_all(students)
            session.flush()

            session.add_all([
                StudentAttendance(
                    student_id=students[0].id,
                    date=date(2026, 3, 12),
                    status="出席",
                    recorded_by=user.id,
                ),
                StudentAttendance(
                    student_id=students[1].id,
                    date=date(2026, 3, 12),
                    status="病假",
                    recorded_by=user.id,
                ),
                StudentAttendance(
                    student_id=students[2].id,
                    date=date(2026, 3, 12),
                    status="遲到",
                    recorded_by=user.id,
                ),
            ])
            session.commit()

            overview = build_daily_classroom_overview(session, date(2026, 3, 12))

            assert overview["totals"]["total_students"] == 5
            assert overview["totals"]["recorded_count"] == 3
            assert overview["totals"]["leave_count"] == 1
            assert overview["totals"]["attendance_rate"] == 40.0

            sun_row = next(item for item in overview["classrooms"] if item["classroom_name"] == "向日葵班")
            assert sun_row["student_count"] == 2
            assert sun_row["recorded_count"] == 2
            assert sun_row["leave_count"] == 1
            assert sun_row["attendance_rate"] == 50.0
            assert sun_row["rollcall_status"] == "complete"
            assert sun_row["last_recorded_by"] == "admin_rollcall"

            moon_row = next(item for item in overview["classrooms"] if item["classroom_name"] == "月亮班")
            assert moon_row["student_count"] == 1
            assert moon_row["recorded_count"] == 1
            assert moon_row["rollcall_status"] == "complete"

            star_row = next(item for item in overview["classrooms"] if item["classroom_name"] == "星星班")
            assert star_row["student_count"] == 2
            assert star_row["recorded_count"] == 0
            assert star_row["unmarked_count"] == 2
            assert star_row["rollcall_status"] == "unstarted"
        finally:
            session.close()

    def test_overview_returns_partial_status_and_filters_inactive_students(self):
        session = self.Session()
        try:
            classroom = Classroom(name="海豚班", is_active=True)
            session.add(classroom)
            session.flush()

            active_student = Student(student_id="D001", name="小海", classroom_id=classroom.id, is_active=True)
            second_active_student = Student(student_id="D004", name="小藍", classroom_id=classroom.id, is_active=True)
            inactive_student = Student(student_id="D002", name="小豚", classroom_id=classroom.id, is_active=False)
            future_student = Student(
                student_id="D003",
                name="小魚",
                classroom_id=classroom.id,
                is_active=True,
                enrollment_date=date(2026, 4, 1),
            )
            session.add_all([active_student, second_active_student, inactive_student, future_student])
            session.flush()

            session.add(StudentAttendance(
                student_id=active_student.id,
                date=date(2026, 3, 12),
                status="缺席",
            ))
            session.commit()

            overview = build_daily_classroom_overview(session, date(2026, 3, 12))
            row = overview["classrooms"][0]

            assert row["student_count"] == 2
            assert row["recorded_count"] == 1
            assert row["unmarked_count"] == 1
            assert row["absent_count"] == 1
            assert row["rollcall_status"] == "partial"
        finally:
            session.close()
