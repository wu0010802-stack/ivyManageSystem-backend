"""薪資節慶獎金明細回歸測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, ClassGrade, Classroom, Employee, Student
from services.salary_engine import SalaryEngine


@pytest.fixture
def salary_engine_db(tmp_path):
    """建立隔離 sqlite DB，驗證 salary breakdown 的實際查詢路徑。"""
    db_path = tmp_path / "salary-breakdown-regressions.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    yield SalaryEngine(load_from_db=False), session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _create_teacher(
    session,
    *,
    employee_id: str,
    name: str,
    title: str,
    position: str,
    hire_date: date,
) -> Employee:
    teacher = Employee(
        employee_id=employee_id,
        name=name,
        title=title,
        position=position,
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=hire_date,
        is_active=True,
    )
    session.add(teacher)
    session.flush()
    return teacher


def _create_students(
    session,
    classroom_id: int,
    count: int,
    prefix: str,
    *,
    enrollment_date: date | None = None,
    graduation_date: date | None = None,
    is_active: bool = True,
):
    for idx in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{idx:03d}",
                name=f"{prefix}學生{idx}",
                classroom_id=classroom_id,
                enrollment_date=enrollment_date,
                graduation_date=graduation_date,
                is_active=is_active,
            )
        )


class TestFestivalEligibilityReferenceDate:
    def test_calculate_salary_uses_salary_month_reference_date(self, engine):
        employee = {
            "employee_id": "E900",
            "name": "六月應符合資格",
            "title": "幼兒園教師",
            "position": "幼兒園教師",
            "employee_type": "regular",
            "base_salary": 30000,
            "hourly_rate": 0,
            "insurance_salary": 30000,
            "dependents": 0,
            "hire_date": "2026-03-01",
        }
        classroom_context = {
            "role": "head_teacher",
            "grade_name": "大班",
            "current_enrollment": 24,
            "has_assistant": True,
            "is_shared_assistant": False,
        }

        breakdown = engine.calculate_salary(
            employee=employee,
            year=2026,
            month=6,
            classroom_context=classroom_context,
        )

        assert breakdown.festival_bonus == 2000


class TestFestivalBonusBreakdownRegressions:
    def test_uses_month_end_enrollment_for_festival_and_overtime_bonus(
        self, salary_engine_db
    ):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T905A",
                name="月底副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T905",
                name="月底老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="月底班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id

            _create_students(
                session,
                classroom.id,
                26,
                "END",
                enrollment_date=date(2025, 8, 1),
                is_active=True,
            )
            _create_students(
                session,
                classroom.id,
                1,
                "GRAD",
                enrollment_date=date(2025, 8, 1),
                graduation_date=date(2026, 6, 15),
                is_active=True,
            )
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)
        salary = engine.process_salary_calculation(teacher_id, 2026, 6)

        assert result["currentEnrollment"] == 26
        assert result["festivalBonus"] == 2167
        assert salary.festival_bonus == 2167
        assert salary.overtime_bonus == 400

    def test_breakdown_uses_salary_month_reference_date(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T900A",
                name="六月副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T900",
                name="六月老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2026, 3, 1),
            )
            classroom = Classroom(
                name="六月班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(session, classroom.id, 24, "JUN")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        assert result["festivalBonus"] == 2000
        assert result["remark"] != "未滿3個月"

    def test_breakdown_art_teacher_matches_salary_engine(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="中班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T901A",
                name="中班副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T901",
                name="美語老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="美語班",
                grade_id=grade.id,
                assistant_teacher_id=assistant_teacher.id,
                art_teacher_id=teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(session, classroom.id, 18, "ART")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)
        salary = engine.calculate_salary(
            employee={
                "employee_id": "T901",
                "name": "美語老師",
                "title": "幼兒園教師",
                "position": "幼兒園教師",
                "employee_type": "regular",
                "base_salary": 30000,
                "hourly_rate": 0,
                "insurance_salary": 30000,
                "dependents": 0,
                "hire_date": "2025-01-01",
            },
            year=2026,
            month=6,
            classroom_context={
                "role": "art_teacher",
                "grade_name": "中班",
                "current_enrollment": 18,
                "has_assistant": True,
                "is_shared_assistant": False,
            },
        )

        assert result["festivalBonus"] == salary.festival_bonus == 2000

    def test_breakdown_shared_assistant_averages_two_classes(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            teacher = _create_teacher(
                session,
                employee_id="T902",
                name="共用副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            first_classroom = Classroom(
                name="共用甲班",
                grade_id=grade.id,
                assistant_teacher_id=teacher.id,
                is_active=True,
            )
            second_classroom = Classroom(
                name="共用乙班",
                grade_id=grade.id,
                assistant_teacher_id=teacher.id,
                is_active=True,
            )
            session.add(first_classroom)
            session.add(second_classroom)
            session.flush()
            teacher.classroom_id = first_classroom.id
            _create_students(session, first_classroom.id, 20, "SHA")
            _create_students(session, second_classroom.id, 16, "SHB")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        # 加權平均：base=1200, 甲班 enroll=20(score 1200), 乙班 enroll=16(score 960)
        # 加權 = (1200*20 + 960*16) / 36 = 39360 / 36 ≈ 1093
        assert result["festivalBonus"] == round((1200 * 20 + 960 * 16) / (20 + 16))
