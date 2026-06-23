"""P2-2 / P2-3 回歸（2026-06-23 深度 audit）：

P2-3 — check_course_capacity 的 NULL fallback 用 999，與全系統 DEFAULT_COURSE_CAPACITY=30
口徑不一致。修：NULL 視為 30。

P2-2 — 終態（離校/畢業/轉出，Student.is_active=False）學生守衛在升 enrolled/promoted_pending
路徑缺失 → 幽靈 enrolled（佔容量卻不出現在點名）。confirm_waitlist_promotion 已有守衛，
本測試補 promote_waitlist（手動直升 enrolled）與 _auto_promote_first_waitlist（自動升
promoted_pending）兩條路徑。

使用 SQLite in-memory，不依賴 dev DB。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from models.classroom import Student
from services.activity_service import ActivityService, DEFAULT_COURSE_CAPACITY


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


def _add_course(session, name="測試課程", capacity=None) -> ActivityCourse:
    c = ActivityCourse(name=name, price=1000, allow_waitlist=True)
    session.add(c)
    session.flush()
    c.capacity = capacity  # flush 後明確設，繞過 column default=30
    session.flush()
    return c


def _add_student(session, name="學生", is_active=True) -> Student:
    st = Student(
        student_id=f"S{name}",
        name=name,
        birthday=date(2020, 1, 1),
        is_active=is_active,
    )
    session.add(st)
    session.flush()
    return st


def _add_reg(session, student_name="測試學生", student_id=None) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone="0912345678",
        student_id=student_id,
        is_paid=False,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(session, reg_id, course_id, status="enrolled") -> RegistrationCourse:
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return rc


# ------------------------------------------------------------------ #
# P2-3：check_course_capacity NULL → 30（非 999）
# ------------------------------------------------------------------ #


def test_check_course_capacity_null_treated_as_default_30(session, svc):
    """capacity=NULL 課程，check_course_capacity 回傳 capacity 應為 30（DEFAULT_COURSE_CAPACITY），
    非 999。has_vacancy 也須以 30 為上限判定。"""
    course = _add_course(session, capacity=None)
    # 佔 30 位
    for i in range(30):
        reg = _add_reg(session, student_name=f"佔位{i}")
        _enroll(session, reg.id, course.id, status="enrolled")

    capacity, occupying, has_vacancy = svc.check_course_capacity(session, course.id)
    assert (
        capacity == DEFAULT_COURSE_CAPACITY
    ), f"NULL capacity 應視為 30，實得 {capacity}"
    assert occupying == 30
    assert has_vacancy is False, "佔位 30、上限 30 時不應再有名額"


# ------------------------------------------------------------------ #
# P2-2：終態學生守衛
# ------------------------------------------------------------------ #


def test_promote_waitlist_refuses_terminal_student(session, svc):
    """手動 promote_waitlist 對 student_id 指向已離校（is_active=False）學生的候補，
    不得升為 enrolled（避免永久幽靈 enrolled）。"""
    course = _add_course(session, capacity=30)
    terminal_student = _add_student(session, name="已離校生", is_active=False)
    reg = _add_reg(session, student_name="已離校生", student_id=terminal_student.id)
    rc = _enroll(session, reg.id, course.id, status="waitlist")

    with pytest.raises(ValueError):
        svc.promote_waitlist(session, reg.id, course.id)

    session.refresh(rc)
    assert rc.status == "waitlist", "終態學生候補不得被升為 enrolled"


def test_auto_promote_skips_terminal_student_promotes_next_active(session, svc):
    """自動遞補時應跳過 student_id 指向已離校學生的候補，改升下一位在籍/校外候補。"""
    course = _add_course(session, capacity=1)  # 僅 1 名額，但目前 0 佔位

    terminal_student = _add_student(session, name="離校候補", is_active=False)
    reg_terminal = _add_reg(
        session, student_name="離校候補", student_id=terminal_student.id
    )
    rc_terminal = _enroll(session, reg_terminal.id, course.id, status="waitlist")

    # 較晚（id 較大）的在籍候補
    active_student = _add_student(session, name="在籍候補", is_active=True)
    reg_active = _add_reg(
        session, student_name="在籍候補", student_id=active_student.id
    )
    rc_active = _enroll(session, reg_active.id, course.id, status="waitlist")

    svc._auto_promote_first_waitlist(session, course.id)
    session.flush()

    session.refresh(rc_terminal)
    session.refresh(rc_active)
    assert rc_terminal.status == "waitlist", "終態學生候補不得被自動升位"
    assert rc_active.status == "promoted_pending", "應跳過終態候補、改升下一位在籍候補"
