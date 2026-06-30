"""tests/test_employees.py — 員工建立自動配號測試。

涵蓋：
- POST /api/employees 不帶 employee_id → 成功建立，工號由 server 自動配發
- 自動配號格式符合 {民國年:03d}{流水:03d}
- 連續建立兩筆，工號不同
- 新欄位 gender/email/insurance_effective_date 建立後讀回
- 兩段式建檔：不填薪資欄仍可成功 / 編輯可清空新欄位
"""

import os
import re
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
from api.employees import router as employees_router
from models.base import Base
from models.database import User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def employees_client(tmp_path):
    db_path = tmp_path / "employees.sqlite"
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
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(employees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, session_factory):
    """建立 admin 帳號（無 employee_id）並登入，回傳 admin user id。"""
    with session_factory() as s:
        u = User(
            username="admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["EMPLOYEES_READ", "EMPLOYEES_WRITE"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(u)
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Temp123456"}
    )
    assert resp.status_code == 200, resp.json()


def test_create_employee_auto_assigns_employee_id(employees_client):
    """POST 不帶 employee_id → 建立成功，回傳自動配發的工號。"""
    client, sf = employees_client
    _login_admin(client, sf)

    payload = {
        "name": "甲",
        "employee_type": "regular",
    }
    resp = client.post("/api/employees", json=payload)
    assert resp.status_code == 201, resp.json()
    data = resp.json()
    assert "employee_id" in data
    # 格式：6 位數字，前 3 碼為民國年（≥100），後 3 碼為流水
    assert re.fullmatch(
        r"\d{6,}", data["employee_id"]
    ), f"工號格式不符：{data['employee_id']!r}"


def test_create_employee_sequential_ids_are_different(employees_client):
    """連續建立兩筆員工，工號不同且流水遞增。"""
    client, sf = employees_client
    _login_admin(client, sf)

    payload1 = {"name": "甲", "employee_type": "regular"}
    payload2 = {"name": "乙", "employee_type": "regular"}

    resp1 = client.post("/api/employees", json=payload1)
    assert resp1.status_code == 201, resp1.json()
    resp2 = client.post("/api/employees", json=payload2)
    assert resp2.status_code == 201, resp2.json()

    id1 = resp1.json()["employee_id"]
    id2 = resp2.json()["employee_id"]
    assert id1 != id2, "連續建立兩筆工號應不同"
    # 同年同前綴，流水後 3 碼遞增
    assert id1[:3] == id2[:3], "同年到職工號前綴應相同"
    assert int(id2[3:]) == int(id1[3:]) + 1, "流水應依序遞增"


def test_create_employee_with_hire_date_uses_roc_year(employees_client):
    """帶 hire_date 建立員工，工號前 3 碼應為民國到職年。"""
    client, sf = employees_client
    _login_admin(client, sf)

    payload = {
        "name": "丙",
        "employee_type": "regular",
        "hire_date": "2025-09-01",  # 民國 114 年
    }
    resp = client.post("/api/employees", json=payload)
    assert resp.status_code == 201, resp.json()
    eid = resp.json()["employee_id"]
    assert eid.startswith(
        "114"
    ), f"hire_date 2025-09-01 → 民國 114 年，工號應以 114 開頭，實為 {eid!r}"


def test_create_employee_with_new_fields(employees_client):
    """新增員工帶 gender / email / insurance_effective_date，能存能讀回。"""
    client, sf = employees_client
    _login_admin(client, sf)

    payload = {
        "name": "新欄位測試員",
        "employee_type": "regular",
        "gender": "女",
        "email": "newfield@example.com",
        "insurance_effective_date": "2026-07-01",
    }
    r = client.post("/api/employees", json=payload)
    assert r.status_code == 201, r.text
    emp_id = r.json()["id"]

    detail = client.get(f"/api/employees/{emp_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["gender"] == "女"
    assert body["email"] == "newfield@example.com"
    assert body["insurance_effective_date"] == "2026-07-01"


def test_create_employee_without_salary_two_stage(employees_client):
    """兩段式回歸：不帶任何薪資欄位仍可建檔成功。"""
    client, sf = employees_client
    _login_admin(client, sf)

    r = client.post(
        "/api/employees",
        json={"name": "只建人不填薪資", "employee_type": "regular"},
    )
    assert r.status_code == 201, r.text


def test_update_employee_can_clear_new_fields(employees_client):
    """編輯時把 gender / email / insurance_effective_date 設為 null 可清空。"""
    client, sf = employees_client
    _login_admin(client, sf)

    created = client.post(
        "/api/employees",
        json={
            "name": "可清空測試",
            "employee_type": "regular",
            "gender": "男",
            "email": "clearme@example.com",
            "insurance_effective_date": "2026-08-01",
        },
    )
    assert created.status_code == 201, created.text
    emp_id = created.json()["id"]

    upd = client.put(
        f"/api/employees/{emp_id}",
        json={"gender": None, "email": None, "insurance_effective_date": None},
    )
    assert upd.status_code == 200, upd.text

    body = client.get(f"/api/employees/{emp_id}").json()
    assert body["gender"] is None
    assert body["email"] is None
    assert body["insurance_effective_date"] is None


def test_employees_search_multi_token(employees_client):
    """多關鍵字搜尋：空格分隔多 token，AND 語義，只回符合全部 token 的員工。"""
    client, sf = employees_client
    _login_admin(client, sf)

    client.post("/api/employees", json={"name": "林美麗", "employee_type": "regular"})
    client.post("/api/employees", json={"name": "林大同", "employee_type": "regular"})

    resp = client.get("/api/employees", params={"search": "林 美"})
    assert resp.status_code == 200, resp.json()
    names = [e["name"] for e in resp.json()]
    assert "林美麗" in names, f"期待 '林美麗' 出現於結果，實得 {names}"
    assert "林大同" not in names, f"期待 '林大同' 不出現於結果，實得 {names}"


def test_employees_search_wildcard_escaped(employees_client):
    """搜尋含 '%' 時應被跳脫，不能當萬用字元拉全表。"""
    client, sf = employees_client
    _login_admin(client, sf)

    client.post("/api/employees", json={"name": "林美麗", "employee_type": "regular"})

    resp = client.get("/api/employees", params={"search": "%"})
    assert resp.status_code == 200, resp.json()
    # 修前：'%' 當萬用字元 → 拉全部；修後：跳脫後 0 命中
    assert resp.json() == [], f"搜尋 '%' 應回空列表，實得 {resp.json()}"
