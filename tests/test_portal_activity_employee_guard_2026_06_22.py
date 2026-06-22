"""tests/test_portal_activity_employee_guard_2026_06_22.py

Finding 2（code review）：portal 點名端點
  GET /api/portal/activity/attendance/sessions/{id}
  PUT /api/portal/activity/attendance/sessions/{id}/records
只 Depends(get_current_user)，從未呼叫 _get_employee。router 層只用
require_non_parent_role 排除家長，沒有驗證 Employee／教師身分 → 任何非家長帳號
（含無員工關聯的服務／管理帳號）都能讀全校跨班名冊並修改出席，而出席會影響退費比例。

口徑（業主裁）：兩端點加 _get_employee 守衛（要求 employee 身分），與同檔
get_portal_activity_registrations 一致；require_non_parent_role docstring 本即
要求 endpoint 仍呼叫 _get_employee。
"""

import os
import sys
from datetime import date

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
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.database import Base, Employee, User
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal_emp_guard.sqlite"
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


def _login(c, username):
    r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def _seed(sf):
    """一場次 + 一筆報名；兩個非家長帳號：
    - no_emp_staff：role=admin，NO employee_id（非家長但無員工關聯）
    - emp_teacher ：role=teacher，有 employee_id
    皆持 STUDENTS_READ（排除 PII 遮罩干擾，聚焦『能否存取』本身）。
    """
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

        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-01-01",
            class_name="大班",
            is_active=True,
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

        sess = ActivitySession(
            course_id=course.id, session_date=date.today(), created_by="seed"
        )
        s.add(sess)
        s.flush()

        s.add(
            User(
                username="no_emp_staff",
                password_hash=hash_password(PASSWORD),
                role="admin",
                employee_id=None,
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
                is_active=True,
                must_change_password=False,
            )
        )
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
        return {"session": sess.id, "reg": reg.id}


class TestPortalActivityEmployeeGuard:
    def test_no_employee_staff_cannot_read_session_detail(self, portal_client):
        """無員工關聯的非家長帳號讀場次詳情 → 403（修前 200 洩漏跨班名冊）。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "no_emp_staff")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 403, res.text

    def test_no_employee_staff_cannot_write_attendance(self, portal_client):
        """無員工關聯的非家長帳號改出席 → 403（出席影響退費比例）。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "no_emp_staff")
        res = c.put(
            f"/api/portal/activity/attendance/sessions/{ids['session']}/records",
            json={"records": [{"registration_id": ids["reg"], "is_present": True}]},
        )
        assert res.status_code == 403, res.text

    def test_employee_teacher_can_read_session_detail(self, portal_client):
        """有員工關聯的教師仍可讀場次詳情 → 200（不誤殺正常教師）。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "emp_teacher")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text

    def test_employee_teacher_can_write_attendance(self, portal_client):
        """有員工關聯的教師仍可改出席 → 200。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "emp_teacher")
        res = c.put(
            f"/api/portal/activity/attendance/sessions/{ids['session']}/records",
            json={"records": [{"registration_id": ids["reg"], "is_present": True}]},
        )
        assert res.status_code == 200, res.text
