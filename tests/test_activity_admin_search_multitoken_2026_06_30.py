"""tests/test_activity_admin_search_multitoken_2026_06_30.py

後台審核學生搜尋（/api/activity/students/search）多關鍵字 AND 測試。

fixture / seed 慣例對齊 test_activity_registration_search_guardian_pii_2026_06_23.py：
- SQLite in-memory 替換 base_module._engine / _SessionFactory
- 建 FastAPI 小 app：auth_router + activity_router
- TestClient 登入取 cookie 後呼叫端點
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.classroom import Student
from models.database import Base
from models.database import User
from utils.auth import hash_password

_PASSWORD = "Temp123456"

# 必要權限：ACTIVITY_WRITE（路由守衛） + STUDENTS_READ（F-027 PII gate）
_PERMS = ["ACTIVITY_WRITE", "STUDENTS_READ"]


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multitoken.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed(sf, username, perms):
    with sf() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password(_PASSWORD),
                role="hr",
                permission_names=perms,
                is_active=True,
            )
        )
        # 兩筆學生：林美麗（同時含「林」與「美」）、林大同（只含「林」）
        s.add(
            Student(
                student_id="T001",
                name="林美麗",
                is_active=True,
            )
        )
        s.add(
            Student(
                student_id="T002",
                name="林大同",
                is_active=True,
            )
        )
        s.commit()


def _login(c, username):
    r = c.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert r.status_code == 200, r.text


def test_admin_search_students_multi_token(client):
    """「林 美」兩個 token AND：只命中林美麗，不命中林大同。"""
    c, sf = client
    _seed(sf, "admin_mt", _PERMS)
    _login(c, "admin_mt")

    resp = c.get("/api/activity/students/search", params={"q": "林 美"})
    assert resp.status_code == 200, resp.text
    names = [s["name"] for s in resp.json()["items"]]
    assert "林美麗" in names, f"林美麗 應在結果中，got {names}"
    assert "林大同" not in names, f"林大同 不應在結果中（不含「美」），got {names}"
