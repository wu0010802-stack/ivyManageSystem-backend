from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.classroom import Classroom, Student, StudentAttendance
from models.event import Holiday
from services.student_attendance_report import build_monthly_attendance_report


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
