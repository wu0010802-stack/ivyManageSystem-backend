"""tests/test_activity_course_sessions_guard_2026_06_22.py

Finding 1（code review）：CourseUpdate.sessions 接受 null，update_course 直接覆寫，
退費計算遇 sessions=NULL 即建議全額退款。只有 ACTIVITY_WRITE 的一線員工可先把
已有報名課程的總堂數清空（或調整），使 build_refund_suggestion 建議全退，再以
「實退≈建議」通過退款偏離閘 + 小額累積閘 → 繞過 ACTIVITY_PAYMENT_APPROVE 盜退。

口徑（業主裁）：課程已有報名／出席紀錄時，變更 sessions 需 ACTIVITY_PAYMENT_APPROVE；
無報名的課程可自由調整。
"""

import os
import sys

import pytest
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
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def course_client(tmp_path):
    db_path = tmp_path / "course_sessions.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
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
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(c, username, password=PASSWORD):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def _add_user(s, username, perms):
    s.add(
        User(
            username=username,
            password_hash=hash_password(PASSWORD),
            role="activity_clerk",
            permission_names=perms,
            is_active=True,
        )
    )


def _make_course(s, *, sessions=12, with_registration=True):
    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name="圍棋",
        price=1000,
        sessions=sessions,
        capacity=30,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    s.add(course)
    s.flush()
    if with_registration:
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-01-01",
            class_name="大班",
            is_active=True,
            paid_amount=1000,
            school_year=sy,
            semester=sem,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
    s.flush()
    return course.id


WRITE_ONLY = ["ACTIVITY_READ", "ACTIVITY_WRITE"]
WITH_APPROVE = ["ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


class TestCourseSessionsGuard:
    def test_write_only_clearing_sessions_with_registrations_blocked(
        self, course_client
    ):
        """ACTIVITY_WRITE 清空已有報名課程的 sessions → 403（防退費盜領）。"""
        c, sf = course_client
        with sf() as s:
            _add_user(s, "clerk", WRITE_ONLY)
            cid = _make_course(s, sessions=12, with_registration=True)
            s.commit()
        _login(c, "clerk")
        res = c.put(f"/api/activity/courses/{cid}", json={"sessions": None})
        assert res.status_code == 403, res.text
        # 確認未被寫入
        with sf() as s:
            course = s.query(ActivityCourse).get(cid)
            assert course.sessions == 12, "sessions 不應被寫入"

    def test_write_only_changing_sessions_with_registrations_blocked(
        self, course_client
    ):
        """ACTIVITY_WRITE 調整（非清空）已有報名課程的 sessions → 一樣 403。"""
        c, sf = course_client
        with sf() as s:
            _add_user(s, "clerk", WRITE_ONLY)
            cid = _make_course(s, sessions=12, with_registration=True)
            s.commit()
        _login(c, "clerk")
        res = c.put(f"/api/activity/courses/{cid}", json={"sessions": 1})
        assert res.status_code == 403, res.text

    def test_payment_approve_can_change_sessions_with_registrations(
        self, course_client
    ):
        """具 ACTIVITY_PAYMENT_APPROVE 者可變更已有報名課程的 sessions → 200。"""
        c, sf = course_client
        with sf() as s:
            _add_user(s, "boss", WITH_APPROVE)
            cid = _make_course(s, sessions=12, with_registration=True)
            s.commit()
        _login(c, "boss")
        res = c.put(f"/api/activity/courses/{cid}", json={"sessions": None})
        assert res.status_code == 200, res.text
        with sf() as s:
            course = s.query(ActivityCourse).get(cid)
            assert course.sessions is None

    def test_write_only_can_change_sessions_without_registrations(self, course_client):
        """無報名的課程，ACTIVITY_WRITE 可自由調整 sessions → 200。"""
        c, sf = course_client
        with sf() as s:
            _add_user(s, "clerk", WRITE_ONLY)
            cid = _make_course(s, sessions=12, with_registration=False)
            s.commit()
        _login(c, "clerk")
        res = c.put(f"/api/activity/courses/{cid}", json={"sessions": 8})
        assert res.status_code == 200, res.text
        with sf() as s:
            course = s.query(ActivityCourse).get(cid)
            assert course.sessions == 8

    def test_write_only_unchanged_sessions_with_registrations_allowed(
        self, course_client
    ):
        """更新其他欄位、sessions 帶相同值（未實際變更）→ 不被閘擋（避免過度封鎖）。"""
        c, sf = course_client
        with sf() as s:
            _add_user(s, "clerk", WRITE_ONLY)
            cid = _make_course(s, sessions=12, with_registration=True)
            s.commit()
        _login(c, "clerk")
        res = c.put(
            f"/api/activity/courses/{cid}",
            json={"sessions": 12, "description": "更新說明"},
        )
        assert res.status_code == 200, res.text
