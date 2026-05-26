"""router-side helper build_refund_suggestion 測試。

涵蓋 spec §6 + §10 邊界：
- 多 course + supply 組裝
- ActivityCourse.sessions=NULL → suggested=None + amount_due fallback
- waitlist / promoted_pending 略過
- is_present=False 不算 T_served
- 軟刪課程仍納入歷史 reg
- reg 不存在 → ValueError
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
)
from services.activity_refund_query import build_refund_suggestion


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "refund_query.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _create_course(session, *, name="美術", sessions=10, price=1500):
    c = ActivityCourse(
        name=name,
        price=price,
        sessions=sessions,
        capacity=30,
        school_year=114,
        semester=1,
    )
    session.add(c)
    session.flush()
    return c


def _create_supply(session, *, name="畫具包", price=500):
    sup = ActivitySupply(
        name=name,
        price=price,
        school_year=114,
        semester=1,
    )
    session.add(sup)
    session.flush()
    return sup


def _create_reg(session, **kwargs):
    reg = ActivityRegistration(
        student_name=kwargs.get("student_name", "王小明"),
        birthday="2020-01-01",
        class_name="大班",
        school_year=114,
        semester=1,
        paid_amount=kwargs.get("paid_amount", 2000),
        is_paid=True,
        is_active=True,
    )
    session.add(reg)
    session.flush()
    return reg


def _attend_n_sessions(session, reg_id, course_id, n, is_present=True):
    """在 course 下建 n 個 ActivitySession 並對 reg 點到 n 筆 is_present 紀錄。"""
    for i in range(n):
        sess = ActivitySession(course_id=course_id, session_date=date(2026, 5, i + 1))
        session.add(sess)
        session.flush()
        att = ActivityAttendance(
            session_id=sess.id,
            registration_id=reg_id,
            is_present=is_present,
        )
        session.add(att)
    session.flush()


def test_reg_not_found_raises(db_session):
    """reg 不存在 → ValueError（呼叫端應在 endpoint 層轉 404）。"""
    with pytest.raises(ValueError, match="not found"):
        build_refund_suggestion(db_session, reg_id=99999)


def test_basic_reg_with_one_course_one_supply(db_session):
    """reg 含 1 課程 10 堂上 1 堂 + 1 用品 → course 退 2/3、supply 0。"""
    course = _create_course(db_session, sessions=10, price=1500)
    supply = _create_supply(db_session, price=500)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.add(
        RegistrationSupply(
            registration_id=reg.id,
            supply_id=supply.id,
            price_snapshot=500,
        )
    )
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course.id, n=1)

    result = build_refund_suggestion(db_session, reg.id)

    assert result["registration_id"] == reg.id
    assert len(result["items"]) == 2
    course_item = next(it for it in result["items"] if it["type"] == "course")
    supply_item = next(it for it in result["items"] if it["type"] == "supply")
    assert course_item["suggested_amount"] == 1000  # 1500 × 2/3
    assert course_item["calc_payload"]["T_served"] == 1
    assert course_item["calc_payload"]["T_total"] == 10
    assert supply_item["suggested_amount"] == 0
    # total = course suggested + supply suggested (0)
    assert result["total_suggested_amount"] == 1000
    assert result["total_amount_due"] == 2000


def test_course_sessions_null_fallback_to_amount_due(db_session):
    """ActivityCourse.sessions IS NULL → item.suggested_amount=None + warning；
    total_suggested 以 amount_due fallback（保守當全退）。"""
    course = _create_course(db_session, sessions=None, price=1500)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)

    course_item = result["items"][0]
    assert course_item["suggested_amount"] is None
    assert any("總堂數" in w for w in course_item["warnings"])
    # total 用 amount_due fallback（spec §6 算法）
    assert result["total_suggested_amount"] == 1500


def test_waitlist_course_skipped(db_session):
    """status != 'enrolled' 的 RegistrationCourse 不出現在 items。"""
    course_enrolled = _create_course(db_session, name="A 課", sessions=10, price=1500)
    course_waitlist = _create_course(db_session, name="B 課", sessions=10, price=2000)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_enrolled.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_waitlist.id,
            status="waitlist",
            price_snapshot=2000,
        )
    )
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_items = [it for it in result["items"] if it["type"] == "course"]
    assert len(course_items) == 1
    assert course_items[0]["target_id"] == course_enrolled.id


def test_zero_attendance_full_refund(db_session):
    """無 attendance → T_served=0 → 全退（not_started 特例）。"""
    course = _create_course(db_session, sessions=10, price=1500)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_item = result["items"][0]
    assert course_item["suggested_amount"] == 1500
    assert course_item["calc_payload"]["ratio_band"] == "not_started"


def test_is_present_false_not_counted(db_session):
    """is_present=False 的 attendance 不算 T_served。"""
    course = _create_course(db_session, sessions=10, price=1500)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course.id, n=5, is_present=False)

    result = build_refund_suggestion(db_session, reg.id)
    course_item = result["items"][0]
    assert course_item["calc_payload"]["T_served"] == 0
    assert course_item["suggested_amount"] == 1500  # not_started 全退


def test_soft_deleted_course_still_included(db_session):
    """ActivityCourse.is_active=False（軟刪）仍在 items 中（歷史 reg 仍要算）。"""
    course = _create_course(db_session, sessions=10, price=1500)
    course.is_active = False
    db_session.flush()
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_items = [it for it in result["items"] if it["type"] == "course"]
    assert len(course_items) == 1


def test_mixed_courses(db_session):
    """2 課程：1 課上 3/10（<1/3 退 2/3）+ 1 課 0/10（全退）。"""
    course_a = _create_course(db_session, name="A", sessions=10, price=1500)
    course_b = _create_course(db_session, name="B", sessions=10, price=2400)
    reg = _create_reg(db_session)
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_a.id,
            status="enrolled",
            price_snapshot=1500,
        )
    )
    db_session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_b.id,
            status="enrolled",
            price_snapshot=2400,
        )
    )
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course_a.id, n=3)

    result = build_refund_suggestion(db_session, reg.id)
    items = {it["target_id"]: it for it in result["items"]}
    # A: served=3/10=0.3 < 1/3 → 退 2/3 of 1500 = 1000
    assert items[course_a.id]["suggested_amount"] == 1000
    # B: served=0 → 全退 2400
    assert items[course_b.id]["suggested_amount"] == 2400
    assert result["total_suggested_amount"] == 1000 + 2400
