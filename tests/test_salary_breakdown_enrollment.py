from datetime import date

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


def _make_students(session, classroom_id, n=23):
    for i in range(n):
        s = Student(
            student_id=f"S{classroom_id}{i:03d}",
            name=f"學生{i}",
            classroom_id=classroom_id,
            is_active=True,
            enrollment_date=date(2025, 8, 1),
            lifecycle_status="active",
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
        "multi_head": False,
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
    """Students graduated/withdrawn before target_date are excluded from count."""
    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T004", name="班導丁")
    classroom = _make_classroom(session, "大班 C", grade.id, head_id=teacher.id)
    # 20 currently in
    _make_students(session, classroom.id, n=20)
    # 3 who graduated last year — should not count for May 2026 snapshot
    for i in range(3):
        s = Student(
            student_id=f"G{classroom.id}{i:03d}",
            name=f"已畢業{i}",
            classroom_id=classroom.id,
            is_active=True,  # is_active not used by the new helper
            enrollment_date=date(2023, 8, 1),
            graduation_date=date(2025, 7, 31),  # before target 2026-05-31
            lifecycle_status="graduated",
        )
        session.add(s)
    session.flush()

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["total"] == 20


def test_compute_breakdown_no_grade_yields_null_grade_name(test_db_session):
    session = test_db_session
    teacher = _make_teacher(session, code="T005", name="班導戊")
    classroom = _make_classroom(session, "未分級", grade_id=None, head_id=teacher.id)
    _make_students(session, classroom.id, n=10)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["grade_name"] is None


def test_compute_breakdown_picks_current_term_over_other_term(test_db_session):
    """REGRESSION: 老師跨學期帶不同班，breakdown 必須挑「target_date 當期」班級.

    target_date=2026-05-31 → 學年度 114 學期 2；若同 head 在 (114,1) 與 (114,2)
    都有班，必須回 (114,2) 的班，不是 (114,1)。Bug 起源：commit 01bba028 標題說
    對齊 engine date-snapshot 但 head_classroom 沒帶 school_year/semester。
    """
    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T_TERM", name="跨學期老師")
    # 上學期班（不應該被選中）
    c_prev = Classroom(
        name="上學期班",
        school_year=114,
        semester=1,
        grade_id=grade.id,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    # 當期班（應被選中）
    c_curr = Classroom(
        name="當期班",
        school_year=114,
        semester=2,
        grade_id=grade.id,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    session.add_all([c_prev, c_curr])
    session.flush()
    _make_students(session, c_prev.id, n=10)
    _make_students(session, c_curr.id, n=20)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["classroom_name"] == "當期班"
    assert result["enrollment"]["total"] == 20


def test_compute_breakdown_falls_back_when_no_term_match(test_db_session):
    """老師當期無班時 fallback 至跨期任一 active（與 engine 同行為）."""
    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T_FB", name="只有上學期班")
    c = Classroom(
        name="僅上學期班",
        school_year=114,
        semester=1,
        grade_id=grade.id,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    session.add(c)
    session.flush()
    _make_students(session, c.id, n=15)

    # target_date 2026-05-31 → (114,2)；應 fallback
    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result["enrollment"]["classroom_name"] == "僅上學期班"
    assert result["enrollment"]["total"] == 15


def test_compute_breakdown_multi_head_flag(test_db_session, caplog):
    """同一老師同時為多 active head_teacher 時，breakdown 補 multi_head=True 旗標."""
    import logging

    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T_MH", name="多頭老師")
    c1 = _make_classroom(session, "甲班", grade.id, head_id=teacher.id)
    c2 = _make_classroom(session, "乙班", grade.id, head_id=teacher.id)
    _make_students(session, c1.id, n=10)
    _make_students(session, c2.id, n=12)

    with caplog.at_level(logging.WARNING):
        result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    # 取 id 升冪第一個（c1=甲班）
    assert result["enrollment"]["classroom_name"] == "甲班"
    assert result["enrollment"]["multi_head"] is True
    assert any("多個 active 班級" in rec.message for rec in caplog.records)


def test_compute_breakdown_inactive_classroom_excluded(test_db_session):
    """is_active=False classroom must not appear in enrollment or assistant."""
    session = test_db_session
    grade = _make_grade(session)
    teacher = _make_teacher(session, code="T006", name="班導己")
    # Head of an inactive classroom — should not count
    c = Classroom(
        name="廢棄班",
        school_year=2026,
        semester=1,
        grade_id=grade.id,
        head_teacher_id=teacher.id,
        is_active=False,
    )
    session.add(c)
    session.flush()
    _make_students(session, c.id, n=15)

    result = compute_enrollment_breakdown(session, teacher.id, date(2026, 5, 31))

    assert result is None
