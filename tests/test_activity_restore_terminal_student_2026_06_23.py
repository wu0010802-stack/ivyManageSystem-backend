"""P2-2 第三路徑回歸（2026-06-23 深度 audit）：restore_registration 終態學生守衛。

被拒報名（matched 在籍生）restore 時，若該 student 期間已轉終態（is_active=False），
restore 會把報名翻回 is_active=True + 保留 enrolled 課程列 → 幽靈 enrolled：佔容量、
inflate total_enrollments，卻因 Student.is_active=False 不出現在點名/出席統計。

修正：restore 偵測 matched student 為終態時擋下（400），不復原為有效報名。
student_id 為 NULL（校外生）不受影響。

沿用 test_activity_restore_capacity.py 的 SQLite + auth 整合測試模式。
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
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def restore_client(tmp_path):
    db_path = tmp_path / "restore_terminal.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed(session):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
            is_active=True,
        )
    )
    session.add(Classroom(name="大象班", is_active=True, school_year=sy, semester=sem))
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            parent_phone="0912345678",
            is_active=True,
        )
    )
    course = ActivityCourse(
        name="圍棋",
        price=1200,
        capacity=10,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(course)
    session.commit()
    return course.id


def _login(client):
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )
    assert r.status_code == 200, r.text


def _register(client):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": "大象班",
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
        },
    )


def test_restore_blocked_when_matched_student_terminal(restore_client):
    client, sf = restore_client
    with sf() as s:
        _seed(s)

    ra = _register(client)
    assert ra.status_code == 201, ra.text
    reg_id = ra.json()["id"]

    _login(client)
    rj = client.post(
        f"/api/activity/registrations/{reg_id}/reject",
        json={"reason": "測試用拒絕原因"},
    )
    assert rj.status_code == 200, rj.text

    # 學生期間轉終態（離校/畢業/轉出）
    with sf() as s:
        st = s.query(Student).filter_by(student_id="S001").one()
        st.is_active = False
        s.commit()

    # restore 應被擋下（不得復原為有效報名 → 幽靈 enrolled）
    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 400, res.text

    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        assert reg.is_active is False, "終態學生報名不得被 restore 為有效"
