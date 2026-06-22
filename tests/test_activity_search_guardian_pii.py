"""tests/test_activity_search_guardian_pii.py

後台學生搜尋（GET /activity/students/search）家長電話 PII 把關（A1）。

問題：admin_search_students 回傳 parent_phone 只檢查 can_view_student_pii
（STUDENTS_READ），但同模組 registrations / pending 列表把 parent_phone 鎖在
can_view_guardian_pii（GUARDIANS_READ）後。持 ACTIVITY_WRITE + STUDENTS_READ
但無 GUARDIANS_READ 的角色可在此端點取得任意在籍學生家長電話，且能以部分
手機號反查學生（搜尋條件比對 parent_phone / emergency_contact_phone）。

修正後：
- 缺 GUARDIANS_READ → parent_phone 回 None（遮罩，與 registrations 系列一致）
- 缺 GUARDIANS_READ → 搜尋條件不含手機欄位（關閉手機反查側信道）
- 有 GUARDIANS_READ → parent_phone 正常回、可用手機搜尋
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
from models.database import Base, Student
from tests.test_activity_pos import _create_admin, _login

_PHONE = "0912345678"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "search_pii.sqlite"
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


def _seed_student(sf):
    with sf() as s:
        s.add(
            Student(
                student_id="S001",
                name="王小明",
                parent_phone=_PHONE,
                is_active=True,
            )
        )
        s.commit()


def _mk_user(sf, username, perms):
    with sf() as s:
        _create_admin(s, username=username, permission_names=perms)
        s.commit()


def test_search_masks_parent_phone_without_guardians_read(client):
    c, sf = client
    _seed_student(sf)
    _mk_user(sf, "students_only", ["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"])
    assert _login(c, username="students_only").status_code == 200

    res = c.get("/api/activity/students/search", params={"q": "王小明"})
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["parent_phone"] is None, "缺 GUARDIANS_READ 應遮罩家長電話"


def test_search_shows_parent_phone_with_guardians_read(client):
    c, sf = client
    _seed_student(sf)
    _mk_user(
        sf,
        "with_guardian",
        ["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ", "GUARDIANS_READ"],
    )
    assert _login(c, username="with_guardian").status_code == 200

    res = c.get("/api/activity/students/search", params={"q": "王小明"})
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["parent_phone"] == _PHONE


def test_search_by_phone_blocked_without_guardians_read(client):
    c, sf = client
    _seed_student(sf)
    _mk_user(sf, "students_only", ["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"])
    assert _login(c, username="students_only").status_code == 200

    # 以手機號搜尋：缺 GUARDIANS_READ 時手機不在搜尋條件 → 反查不到
    res = c.get("/api/activity/students/search", params={"q": _PHONE})
    assert res.status_code == 200, res.text
    assert res.json()["items"] == [], "缺 GUARDIANS_READ 不應能以手機反查學生"


def test_search_by_phone_allowed_with_guardians_read(client):
    c, sf = client
    _seed_student(sf)
    _mk_user(
        sf,
        "with_guardian",
        ["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ", "GUARDIANS_READ"],
    )
    assert _login(c, username="with_guardian").status_code == 200

    res = c.get("/api/activity/students/search", params={"q": _PHONE})
    assert res.status_code == 200, res.text
    assert len(res.json()["items"]) == 1
