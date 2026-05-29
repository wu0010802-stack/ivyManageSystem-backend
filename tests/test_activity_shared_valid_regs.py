"""query_valid_session_registrations 純查詢 helper 單元測試。

使用 conftest 的 test_db_session fixture（單一 session、全表已建）。
"""

from api.activity._shared import query_valid_session_registrations
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)


_reg_counter = 0


def _mk_reg(
    s,
    *,
    course_id,
    student_id=None,
    is_active=True,
    match_status="manual",
    rc_status="enrolled",
    classroom_id=None,
):
    global _reg_counter
    _reg_counter += 1
    reg = ActivityRegistration(
        student_name=f"生{_reg_counter}",
        birthday="2020-01-01",
        class_name="班",
        is_active=is_active,
        school_year=115,
        semester=1,
        student_id=student_id,
        parent_phone=f"09{_reg_counter:09d}",
        classroom_id=classroom_id,
        match_status=match_status,
        pending_review=False,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_id,
            status=rc_status,
            price_snapshot=100,
        )
    )
    s.flush()
    return reg.id


def test_valid_regs_returns_enrolled_active_only(test_db_session):
    s = test_db_session
    course = ActivityCourse(
        name="圍棋", price=100, school_year=115, semester=1, is_active=True
    )
    s.add(course)
    s.flush()
    good = _mk_reg(s, course_id=course.id)
    inactive = _mk_reg(s, course_id=course.id, is_active=False)
    rejected = _mk_reg(s, course_id=course.id, match_status="rejected")
    waitlist = _mk_reg(s, course_id=course.id, rc_status="waitlist")
    s.commit()

    rows = query_valid_session_registrations(
        s, course.id, [good, inactive, rejected, waitlist]
    )
    assert {r[0] for r in rows} == {good}


def test_valid_regs_classroom_filter(test_db_session):
    s = test_db_session
    course = ActivityCourse(
        name="繪畫", price=100, school_year=115, semester=1, is_active=True
    )
    s.add(course)
    s.flush()
    in_class = _mk_reg(s, course_id=course.id, classroom_id=7)
    other_class = _mk_reg(s, course_id=course.id, classroom_id=9)
    s.commit()
    ids = [in_class, other_class]

    assert {r[0] for r in query_valid_session_registrations(s, course.id, ids)} == {
        in_class,
        other_class,
    }
    assert {
        r[0]
        for r in query_valid_session_registrations(s, course.id, ids, classroom_ids=[7])
    } == {in_class}


def test_valid_regs_empty_input(test_db_session):
    assert query_valid_session_registrations(test_db_session, 1, []) == []
