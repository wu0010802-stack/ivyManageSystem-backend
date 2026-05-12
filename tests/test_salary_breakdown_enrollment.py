from datetime import date
import pytest

from models.classroom import Classroom, ClassGrade, Student
from models.employee import Employee
from services.salary.breakdown_enrollment import compute_enrollment_breakdown


def _make_grade(session, name="大班"):
    g = ClassGrade(name=name)
    session.add(g)
    session.flush()
    return g


def _make_teacher(session, code="T001", name="班導甲"):
    emp = Employee(
        employee_id=code,
        name=name,
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_classroom(session, name, grade_id, head_id=None, asst_id=None, art_id=None):
    c = Classroom(
        name=name,
        school_year=2026,
        semester=1,
        grade_id=grade_id,
        head_teacher_id=head_id,
        assistant_teacher_id=asst_id,
        art_teacher_id=art_id,
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


def _make_students(session, classroom_id, n=23, active=True):
    prefix = "S" if active else "X"
    for i in range(n):
        s = Student(
            student_id=f"{prefix}{classroom_id}{i:03d}",
            name=f"學生{i}",
            classroom_id=classroom_id,
            is_active=active,
            enrollment_date=date(2025, 8, 1),
            lifecycle_status="active" if active else "withdrawn",
        )
        session.add(s)
    session.flush()


def test_compute_breakdown_head_teacher(test_db_session):
    session = test_db_session
    grade = _make_grade(session, "大班")
    teacher = _make_teacher(session)
    classroom = _make_classroom(session, "大班 A", grade.id, head_id=teacher.id)
    _make_students(session, classroom.id, n=23)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result is not None
    assert result["enrollment"] == {
        "snapshot_date": "2026-05-31",
        "total": 23,
        "classroom_id": classroom.id,
        "classroom_name": "大班 A",
        "grade_name": "大班",
    }
    assert result["assistant"] is None


def test_compute_breakdown_assistant_only(test_db_session):
    session = test_db_session
    grade = _make_grade(session, "中班")
    assistant = _make_teacher(session, code="T002", name="副班導乙")
    _make_classroom(session, "中班 A", grade.id, asst_id=assistant.id)
    _make_classroom(session, "中班 B", grade.id, asst_id=assistant.id)

    result = compute_enrollment_breakdown(session, assistant.id, date(2026, 5, 31))

    assert result is not None
    assert result["enrollment"] is None
    assert result["assistant"] == {"by_classroom": ["中班 A", "中班 B"]}


def test_compute_breakdown_head_and_assistant_different_classes(test_db_session):
    session = test_db_session
    grade = _make_grade(session, "大班")
    teacher = _make_teacher(session, code="T003", name="混合丙")
    head = _make_classroom(session, "大班 A", grade.id, head_id=teacher.id)
    _make_students(session, head.id, n=20)
    _make_classroom(session, "大班 B", grade.id, asst_id=teacher.id)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["classroom_name"] == "大班 A"
    assert result["enrollment"]["total"] == 20
    assert result["assistant"] == {"by_classroom": ["大班 B"]}


def test_compute_breakdown_non_teacher_returns_none(test_db_session):
    session = test_db_session
    admin = _make_teacher(session, code="A001", name="行政")

    result = compute_enrollment_breakdown(session, admin.id, date(2026, 5, 31))

    assert result is None


def test_compute_breakdown_inactive_students_excluded(test_db_session):
    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T004", name="班導丁")
    classroom = _make_classroom(session, "大班 C", grade.id, head_id=teacher.id)
    _make_students(session, classroom.id, n=20, active=True)
    _make_students(session, classroom.id, n=3, active=False)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["total"] == 20


def test_compute_breakdown_no_grade_yields_null_grade_name(test_db_session):
    session = test_db_session
    teacher = _make_teacher(session, code="T005", name="班導戊")
    classroom = _make_classroom(session, "未分級", grade_id=None, head_id=teacher.id)
    _make_students(session, classroom.id, n=10)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["grade_name"] is None
