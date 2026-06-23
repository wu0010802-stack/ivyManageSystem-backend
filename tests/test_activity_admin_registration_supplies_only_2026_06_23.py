"""後台手動新增報名的「課程或用品至少一項」invariant 對齊測試。

對齊公開端（schemas/activity_public._require_at_least_one_item）與家長端
（api/parent_portal/activity.register_courses）的核心規則：報名主體至少要有
一門課程或一項用品。後台手動補登流程過去硬性要求課程（`if not body.courses`），
導致用品-only 的合法補登被 400 擋下 → 與公開/家長端口徑漂移。
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
    ActivitySupply,
    Base,
    Classroom,
    RegistrationSupply,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "admin_reg.sqlite"
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
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _setup(session):
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    session.add(Classroom(name="大班", is_active=True))
    sy, sem = resolve_current_academic_term()
    session.add(ActivitySupply(name="畫具包", price=500, school_year=sy, semester=sem))
    session.add(
        ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.flush()


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )


class TestAdminRegistrationItemInvariant:
    def test_supplies_only_registration_succeeds(self, admin_client):
        """用品-only 後台補登應成功（對齊公開/家長端：課程或用品至少一項）。"""
        client, sf = admin_client
        with sf() as s:
            _setup(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "大班",
                "courses": [],
                "supplies": [{"name": "畫具包"}],
            },
        )
        assert res.status_code == 201, res.json()

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_name="王小明").one()
            supplies = (
                s.query(RegistrationSupply).filter_by(registration_id=reg.id).all()
            )
            assert len(supplies) == 1

    def test_empty_registration_still_rejected(self, admin_client):
        """完全空白（無課程、無用品）仍須擋下，避免空殼污染。"""
        client, sf = admin_client
        with sf() as s:
            _setup(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "空白生",
                "birthday": "2020-01-01",
                "class": "大班",
                "courses": [],
                "supplies": [],
            },
        )
        assert res.status_code == 400

    def test_course_only_registration_still_succeeds(self, admin_client):
        """課程-only 仍應成功（不破壞既有路徑）。"""
        client, sf = admin_client
        with sf() as s:
            _setup(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "課程生",
                "birthday": "2020-01-01",
                "class": "大班",
                "courses": [{"name": "美術"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.json()


class TestForcedMatchStatusQuery:
    """forced 是 force-accept 寫入 DB 的既有值，列表查詢契約須承認它。"""

    def _seed_regs(self, sf):
        with sf() as s:
            _setup(s)
            sy, sem = resolve_current_academic_term()
            s.add(
                ActivityRegistration(
                    student_name="強收生",
                    birthday="2020-01-01",
                    class_name="大班",
                    match_status="forced",
                    is_active=True,
                    school_year=sy,
                    semester=sem,
                )
            )
            s.add(
                ActivityRegistration(
                    student_name="配對生",
                    birthday="2020-02-02",
                    class_name="大班",
                    match_status="matched",
                    is_active=True,
                    school_year=sy,
                    semester=sem,
                )
            )
            s.commit()

    def test_filter_by_forced_accepted_and_filters(self, admin_client):
        client, sf = admin_client
        self._seed_regs(sf)
        assert _login(client).status_code == 200

        res = client.get("/api/activity/registrations?match_status=forced")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total"] >= 1
        assert {item["student_name"] for item in body["items"]} == {"強收生"}
        assert all(item["match_status"] == "forced" for item in body["items"])
