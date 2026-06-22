"""Portal 才藝報名列表（教師端）：學期過濾 + 全校候補順位回歸測試。

#7：/portal/activity/registrations 統計未帶學年/學期條件，歷史學期 active 報名
    會混入當前統計（學期輪替為 no-op，舊報名永不失效）。
#8：候補順位只在教師自班 reg 內 enumerate，跨班候補生被排除，顯示的「候補 #N」
    不是全校真實順位（與 admin / 家長端用的全校 window function 口徑不一致）。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portal.activity import router as portal_activity_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Employee,
    RegistrationCourse,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password

PASSWORD = "TempPass123"


@pytest.fixture
def portal_activity_client(tmp_path):
    db_path = tmp_path / "portal-activity.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(portal_activity_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _teacher(session, *, emp_no="T001", username="teacher1"):
    emp = Employee(employee_id=emp_no, name="王老師", base_salary=32000, is_active=True)
    session.add(emp)
    session.flush()
    user = User(
        employee_id=emp.id,
        username=username,
        password_hash=hash_password(PASSWORD),
        role="teacher",
        permission_names=["STUDENTS_READ:own_class"],
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return emp


def _reg(session, *, name, classroom_id, school_year, semester, is_paid=False):
    r = ActivityRegistration(
        student_name=name,
        class_name="班",
        classroom_id=classroom_id,
        school_year=school_year,
        semester=semester,
        is_active=True,
        match_status="matched",
        is_paid=is_paid,
        paid_amount=0,
    )
    session.add(r)
    session.flush()
    return r


def _rc(session, *, registration_id, course_id, status):
    rc = RegistrationCourse(
        registration_id=registration_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return rc


def _login(client, username="teacher1"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r


class TestPortalTermFilter:
    """#7：教師端統計只計當前學期，歷史學期 active 報名不混入。"""

    def test_summary_excludes_other_term_registrations(self, portal_activity_client):
        client, sf = portal_activity_client
        sy, sem = resolve_current_academic_term()
        with sf() as s:
            emp = _teacher(s)
            c1 = Classroom(name="大象班", is_active=True, head_teacher_id=emp.id)
            s.add(c1)
            s.flush()
            course = ActivityCourse(
                name="圍棋", price=1000, capacity=30, is_active=True
            )
            s.add(course)
            s.flush()
            # 當前學期一筆（enrolled）
            cur = _reg(
                s, name="今期生", classroom_id=c1.id, school_year=sy, semester=sem
            )
            _rc(s, registration_id=cur.id, course_id=course.id, status="enrolled")
            # 歷史學期一筆（同班、仍 active）→ 不應混入當前統計
            past = _reg(
                s,
                name="去年生",
                classroom_id=c1.id,
                school_year=sy - 1,
                semester=sem,
            )
            _rc(s, registration_id=past.id, course_id=course.id, status="enrolled")
            s.commit()

        _login(client)
        res = client.get("/api/portal/activity/registrations")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["summary"]["total_registrations"] == 1, "歷史學期報名不應計入"
        assert body["summary"]["total_enrolled"] == 1
        names = [r["student_name"] for r in body["registrations"]]
        assert names == ["今期生"]


class TestPortalWaitlistGlobalPosition:
    """#8：候補順位以全校（同課程跨班）真實順位計算，非只算自班。"""

    def test_waitlist_position_is_school_wide(self, portal_activity_client):
        client, sf = portal_activity_client
        sy, sem = resolve_current_academic_term()
        with sf() as s:
            emp = _teacher(s)
            c1 = Classroom(name="大象班", is_active=True, head_teacher_id=emp.id)
            c2 = Classroom(name="長頸鹿班", is_active=True)  # 他班（教師管不到）
            s.add_all([c1, c2])
            s.flush()
            course = ActivityCourse(
                name="熱門才藝",
                price=1000,
                capacity=1,
                allow_waitlist=True,
                is_active=True,
                school_year=sy,
                semester=sem,
            )
            s.add(course)
            s.flush()

            # 他班學生先候補（rc_id 較小 → 全校順位 #1）
            other = _reg(
                s, name="他班候補生", classroom_id=c2.id, school_year=sy, semester=sem
            )
            _rc(s, registration_id=other.id, course_id=course.id, status="waitlist")
            # 自班學生後候補（rc_id 較大 → 全校順位 #2，但自班內順位 #1）
            mine = _reg(
                s, name="自班候補生", classroom_id=c1.id, school_year=sy, semester=sem
            )
            _rc(s, registration_id=mine.id, course_id=course.id, status="waitlist")
            s.commit()

        _login(client)
        res = client.get("/api/portal/activity/registrations")
        assert res.status_code == 200, res.text
        body = res.json()
        # 教師只看到自班那筆
        own = [r for r in body["registrations"] if r["student_name"] == "自班候補生"]
        assert len(own) == 1, body
        course_entry = own[0]["courses"][0]
        assert course_entry["status"] == "waitlist"
        assert (
            course_entry["waitlist_position"] == 2
        ), "候補順位應為全校真實順位 #2（修前自班內 enumerate 誤報 #1）"
