"""tests/test_activity_add_course_guards_2026_06_23.py

Code review（2026-06-23）兩 finding 回歸：

Finding #1（P1）—— 後台 POST /registrations/{id}/courses（add_registration_course）
缺終態學生守衛。已離校/畢業/轉出（Student.is_active=False）但仍有 active 報名的學生，
後台追加課程會直接寫 RegistrationCourse(status="enrolled") → 幽靈 enrolled：佔容量、
產生欠款，卻因 Student.is_active=False 不出現在點名名冊/統計。對齊
confirm_waitlist_promotion / promote_waitlist / _auto_promote_first_waitlist /
restore 既有的終態守衛。

Finding #2（P2）—— withdraw_course 鎖序為 registration → course（reg 行鎖在前、course
鎖在 _auto_promote_first_waitlist 內才取），與全才藝模組已統一的「course →
registration_course」順序相反。家長端 confirm/decline（先鎖 ActivityCourse、再鎖
RegistrationCourse-join-Registration）與後台退課同時處理同一 (reg, course) 時形成
PostgreSQL ABBA 鎖序反轉死鎖。修：withdraw_course 先鎖 ActivityCourse(course_id)，
RC 查詢補 with_for_update()。

附帶 —— add_registration_course 自身鎖序亦為 reg → course（_lock_registration 在前、
course with_for_update 在後），與 confirm/decline 相反，屬同一 invariant 違反。一併
改為 course → registration。

鎖序測法沿用 test_activity_waitlist_confirm_lock_order_2026_06_23.py：spy
sqlalchemy Query.with_for_update 的呼叫順序，斷言 ActivityCourse 先於
ActivityRegistration / RegistrationCourse 被鎖。SQLite 下 with_for_update 為 no-op
但仍會被呼叫，故可驗證鎖『取得順序』。
"""

import os
import sys
from datetime import date

import pytest
import sqlalchemy.orm
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
    Student,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "add_course_guards.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_admin(session, username="add_admin", password="TempPass123"):
    session.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
            is_active=True,
        )
    )
    session.flush()


def _login(client, username="add_admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return r


def _add_course(session, name="圍棋", capacity=30):
    sy, sem = resolve_current_academic_term()
    c = ActivityCourse(
        name=name,
        price=1000,
        capacity=capacity,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


def _add_student(session, name="學生", is_active=True):
    st = Student(
        student_id=f"S{name}",
        name=name,
        birthday=date(2020, 1, 1),
        is_active=is_active,
    )
    session.add(st)
    session.flush()
    return st


def _add_reg(session, *, student_name="林小華", student_id=None):
    sy, sem = resolve_current_academic_term()
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone="0912345678",
        student_id=student_id,
        is_paid=False,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(r)
    session.flush()
    return r


# ------------------------------------------------------------------ #
# Finding #1：終態學生守衛
# ------------------------------------------------------------------ #


def test_add_course_refuses_terminal_student(client):
    """後台對「已離校（Student.is_active=False）但仍有 active 報名」的學生追加課程，
    應被擋下（400），且不得寫入任何 enrolled RegistrationCourse（避免幽靈 enrolled）。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        terminal = _add_student(s, name="已離校生", is_active=False)
        reg = _add_reg(s, student_name="已離校生", student_id=terminal.id)
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    res = c.post(
        f"/api/activity/registrations/{reg_id}/courses",
        json={"course_id": course_id},
    )
    assert res.status_code == 400, f"終態學生追加課程應 400，實得 {res.status_code}"

    with sf() as s:
        rcs = (
            s.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg_id)
            .all()
        )
        assert rcs == [], "終態學生不得被寫入任何 RegistrationCourse（幽靈 enrolled）"


def test_add_course_allows_active_student(client):
    """反回歸：在籍學生（is_active=True）追加課程仍正常 enrolled。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        active = _add_student(s, name="在籍生", is_active=True)
        reg = _add_reg(s, student_name="在籍生", student_id=active.id)
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    res = c.post(
        f"/api/activity/registrations/{reg_id}/courses",
        json={"course_id": course_id},
    )
    assert res.status_code == 201, f"在籍學生追加課程應 201，實得 {res.status_code}"
    assert res.json()["status"] == "enrolled"

    with sf() as s:
        rc = (
            s.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg_id)
            .one()
        )
        assert rc.status == "enrolled"


def test_add_course_allows_null_student_id(client):
    """反回歸：student_id 為 NULL（校外/未匹配報名）無終態概念，追加課程不受守衛影響。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        reg = _add_reg(s, student_name="校外生", student_id=None)
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    res = c.post(
        f"/api/activity/registrations/{reg_id}/courses",
        json={"course_id": course_id},
    )
    assert (
        res.status_code == 201
    ), f"校外（NULL student_id）追加課程應 201，實得 {res.status_code}"


# ------------------------------------------------------------------ #
# Finding #2 + 附帶：鎖序「course → registration」
# ------------------------------------------------------------------ #


def _spy_locks(monkeypatch):
    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)
    return recorded


def test_withdraw_course_locks_course_before_registration(client, monkeypatch):
    """withdraw_course 須先鎖 ActivityCourse，再鎖 ActivityRegistration /
    RegistrationCourse（對齊 confirm/decline/promote 的 course → registration_course，
    消除與家長端 confirm/decline 並發時的 ABBA 死鎖）。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        reg = _add_reg(s, student_name="退課生", student_id=None)
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    recorded = _spy_locks(monkeypatch)

    res = c.delete(f"/api/activity/registrations/{reg_id}/courses/{course_id}")
    assert res.status_code == 200, f"退課應 200，實得 {res.status_code}：{res.text}"

    locks = [
        e
        for e in recorded
        if e in (ActivityCourse, ActivityRegistration, RegistrationCourse)
    ]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert (
        ActivityRegistration in locks
    ), f"未鎖 ActivityRegistration；recorded={recorded}"
    assert RegistrationCourse in locks, f"未鎖 RegistrationCourse；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(
        ActivityRegistration
    ), f"鎖序錯誤：ActivityCourse 應先於 ActivityRegistration，實際 {locks}"
    assert locks.index(ActivityCourse) < locks.index(
        RegistrationCourse
    ), f"鎖序錯誤：ActivityCourse 應先於 RegistrationCourse，實際 {locks}"


def test_add_course_locks_course_before_registration(client, monkeypatch):
    """add_registration_course 須先鎖 ActivityCourse，再鎖 ActivityRegistration
    （對齊 course → registration_course，消除與家長端 confirm/decline 並發 ABBA）。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        reg = _add_reg(s, student_name="加課生", student_id=None)
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    recorded = _spy_locks(monkeypatch)

    res = c.post(
        f"/api/activity/registrations/{reg_id}/courses",
        json={"course_id": course_id},
    )
    assert res.status_code == 201, f"加課應 201，實得 {res.status_code}：{res.text}"

    locks = [e for e in recorded if e in (ActivityCourse, ActivityRegistration)]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert (
        ActivityRegistration in locks
    ), f"未鎖 ActivityRegistration；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(
        ActivityRegistration
    ), f"鎖序錯誤：ActivityCourse 應先於 ActivityRegistration，實際 {locks}"


def test_withdraw_course_still_withdraws(client):
    """反回歸：重排鎖序後退課本身仍正確（刪除 RegistrationCourse）。"""
    c, sf = client
    with sf() as s:
        _add_admin(s)
        course = _add_course(s)
        reg = _add_reg(s, student_name="退課生2", student_id=None)
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    res = c.delete(f"/api/activity/registrations/{reg_id}/courses/{course_id}")
    assert res.status_code == 200

    with sf() as s:
        rcs = (
            s.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg_id)
            .all()
        )
        assert rcs == [], "退課後該 RegistrationCourse 應被刪除"
