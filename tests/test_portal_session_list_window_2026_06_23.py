"""portal 場次列表無窗保護（2026-06-23 優化）。

portal_list_sessions 無分頁；原本 start_date/end_date 皆未帶時會 .all() 撈全部
歷史場次並對全 session_id 跑出席聚合。加無窗保護：兩個日期都沒帶時預設只回最近
_PORTAL_SESSIONS_DEFAULT_WINDOW_DAYS（365）天場次；顯式帶 start_date 仍可看更早。
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portal import router as portal_router
from models.activity import ActivityCourse, ActivitySession
from models.database import Base, Employee, User
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal_session_window.sqlite"
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
    app.include_router(portal_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed(sf):
    """一課程 + 近窗場次（今天）+ 超窗場次（400 天前）+ 一名教師帳號。"""
    with sf() as s:
        sy, sem = resolve_current_academic_term()
        emp = Employee(
            employee_id="PT001", name="王老師", base_salary=32000, is_active=True
        )
        s.add(emp)
        s.flush()
        course = ActivityCourse(
            name="圍棋",
            price=1000,
            capacity=30,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course)
        s.flush()
        recent = ActivitySession(
            course_id=course.id, session_date=date.today(), created_by="seed"
        )
        old = ActivitySession(
            course_id=course.id,
            session_date=date.today() - timedelta(days=400),
            created_by="seed",
        )
        s.add_all([recent, old])
        s.flush()
        s.add(
            User(
                username="emp_teacher",
                password_hash=hash_password(PASSWORD),
                role="teacher",
                employee_id=emp.id,
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
        return {"recent": recent.id, "old": old.id}


def _login(c):
    r = c.post(
        "/api/auth/login", json={"username": "emp_teacher", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text


def test_no_dates_excludes_sessions_older_than_window(portal_client):
    c, sf = portal_client
    ids = _seed(sf)
    _login(c)
    res = c.get("/api/portal/activity/attendance/sessions")
    assert res.status_code == 200, res.text
    returned = {row["id"] for row in res.json()}
    assert ids["recent"] in returned  # 近窗場次仍在
    assert ids["old"] not in returned  # 超窗（400 天前）被預設窗排除


def test_explicit_old_start_date_includes_old_sessions(portal_client):
    c, sf = portal_client
    ids = _seed(sf)
    _login(c)
    far = (date.today() - timedelta(days=500)).isoformat()
    res = c.get(f"/api/portal/activity/attendance/sessions?start_date={far}")
    assert res.status_code == 200, res.text
    returned = {row["id"] for row in res.json()}
    assert ids["recent"] in returned
    assert ids["old"] in returned  # 顯式帶舊 start_date → 超窗場次也回
