"""tests/test_pos_phone_search_guardian_guard_2026_06_22.py

Finding 4（code review）：GET /pos/outstanding-by-student 只要求 ACTIVITY_READ，
但搜尋關鍵字會比對 parent_phone（家長手機，屬 Guardian PII）。缺 GUARDIANS_READ
的人可逐筆試打電話號碼，從「是否命中某學生」反推電話屬於哪位學生 →
繞過 GUARDIANS_READ 的側信道。

修補（鏡像 registrations_pending.py A1）：缺 GUARDIANS_READ 時，搜尋條件不含
parent_phone 欄位（姓名 / 班級仍可搜）。
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
PARENT_PHONE = "0912345678"


@pytest.fixture
def pos_phone_client(tmp_path):
    db_path = tmp_path / "pos_phone.sqlite"
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


def _seed(sf):
    """一筆欠費報名：王小明，家長手機 0912345678。
    兩個 caller：with_guardian（含 GUARDIANS_READ）/ no_guardian（缺 GUARDIANS_READ）。
    兩者皆持 bare STUDENTS_READ（全園可見、無班級 scoping 干擾）。
    """
    with sf() as s:
        sy, sem = resolve_current_academic_term()
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
            birthday="2020-03-15",
            class_name="大班",
            parent_phone=PARENT_PHONE,
            is_active=True,
            paid_amount=0,  # 欠費 → 出現在 outstanding 清單
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

        s.add(
            User(
                username="with_guardian",
                password_hash=hash_password(PASSWORD),
                role="activity_clerk",
                permission_names=["ACTIVITY_READ", "STUDENTS_READ", "GUARDIANS_READ"],
                is_active=True,
            )
        )
        s.add(
            User(
                username="no_guardian",
                password_hash=hash_password(PASSWORD),
                role="activity_clerk",
                permission_names=["ACTIVITY_READ", "STUDENTS_READ"],
                is_active=True,
            )
        )
        s.commit()


def _phone_search(c):
    return c.get(f"/api/activity/pos/outstanding-by-student?q={PARENT_PHONE}")


class TestPosPhoneSearchGuardianGuard:
    def test_with_guardian_can_search_by_phone(self, pos_phone_client):
        """持 GUARDIANS_READ：用家長手機可搜到學生（既有行為保留）。"""
        c, sf = pos_phone_client
        _seed(sf)
        _login(c, "with_guardian")
        res = _phone_search(c)
        assert res.status_code == 200, res.text
        groups = res.json()["groups"]
        assert len(groups) == 1
        assert groups[0]["student_name"] == "王小明"

    def test_no_guardian_cannot_search_by_phone(self, pos_phone_client):
        """缺 GUARDIANS_READ：用家長手機搜尋不得命中任何學生（關閉反查側信道）。

        修前 → parent_phone 進搜尋條件，命中王小明（可反查電話 → 學生）。
        修後 → 搜尋條件排除 parent_phone，0 groups。"""
        c, sf = pos_phone_client
        _seed(sf)
        _login(c, "no_guardian")
        res = _phone_search(c)
        assert res.status_code == 200, res.text
        groups = res.json()["groups"]
        assert (
            len(groups) == 0
        ), f"缺 GUARDIANS_READ 仍可用手機反查學生（命中 {len(groups)} 筆）"

    def test_no_guardian_can_still_search_by_name(self, pos_phone_client):
        """缺 GUARDIANS_READ 仍可用姓名搜尋（不誤殺正常查詢）。"""
        c, sf = pos_phone_client
        _seed(sf)
        _login(c, "no_guardian")
        res = c.get("/api/activity/pos/outstanding-by-student?q=王小明")
        assert res.status_code == 200, res.text
        groups = res.json()["groups"]
        assert len(groups) == 1
        assert groups[0]["student_name"] == "王小明"
