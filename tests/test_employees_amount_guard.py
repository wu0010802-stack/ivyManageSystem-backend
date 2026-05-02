"""tests/test_employees_amount_guard.py — 員工檔薪資金額欄位修改守衛測試（2026-05-02）。

問題情境：原 EmployeeUpdate 對 base_salary / hourly_rate / insurance_salary_level 只有
ge=0，HR/admin 可直接改 base_salary 繞過 salary manual-adjust 的 reason / 金額上限 /
ACTIVITY_PAYMENT_APPROVE 簽核流程；之後薪資重算就會把惡意金額落入正式薪資。

涵蓋：
- 改他人 base_salary 不帶 adjustment_reason → 400
- 改他人 base_salary 帶 reason 但 delta > 1000 且無 ACTIVITY_PAYMENT_APPROVE → 403
- 改他人 base_salary 帶 reason + ACTIVITY_PAYMENT_APPROVE → 200
- 小額調整（delta ≤ 1000）只要 reason 即可 → 200
- 同值寫入（delta = 0）不觸發守衛 → 200
- 改非金額欄位（phone）不需 reason → 200
- 拆欄合計：base_salary +500 + hourly_rate +600 = 1100 > 1000，無簽核 → 403
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
from api.employees import router as employees_router
from models.base import Base
from models.database import Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def amtguard_client(tmp_path):
    db_path = tmp_path / "amtguard.sqlite"
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
    app.include_router(employees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, *, name, base_salary=30000, hourly_rate=0):
    emp = Employee(
        employee_id=f"E_{name}",
        name=name,
        base_salary=base_salary,
        hourly_rate=hourly_rate,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(session, *, username, permissions, employee_id=None, role="admin"):
    u = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# ── 基本資料修改不受影響 ─────────────────────────────────────────────


def test_non_amount_field_update_no_reason_required(amtguard_client):
    """改 phone（非金額）不需 adjustment_reason → 200。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標A")
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(f"/api/employees/{target_id}", json={"phone": "0911000000"})
    assert res.status_code == 200, res.json()


def test_amount_unchanged_value_not_treated_as_change(amtguard_client):
    """送回相同 base_salary（delta=0）→ 不觸發守衛 → 200。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標B", base_salary=30000)
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(f"/api/employees/{target_id}", json={"base_salary": 30000})
    assert res.status_code == 200, res.json()


# ── 缺原因 / 缺簽核 ──────────────────────────────────────────────────


def test_amount_change_without_reason_400(amtguard_client):
    """改 base_salary 不帶 adjustment_reason → 400 reason missing。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標C", base_salary=30000)
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(f"/api/employees/{target_id}", json={"base_salary": 31500})
    assert res.status_code == 400, res.json()
    assert "原因" in str(res.json().get("detail", ""))


def test_amount_change_above_threshold_without_approve_403(amtguard_client):
    """改 base_salary 帶 reason 但 delta=8000 > 1000 且無 ACTIVITY_PAYMENT_APPROVE → 403。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標D", base_salary=30000)
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(
        f"/api/employees/{target_id}",
        json={"base_salary": 38000, "adjustment_reason": "主管核可加薪"},
    )
    assert res.status_code == 403, res.json()
    assert "金流簽核" in str(res.json().get("detail", ""))


# ── 拆欄繞過：合計門檻 ───────────────────────────────────────────


def test_amount_split_fields_above_threshold_403(amtguard_client):
    """拆欄繞過：base_salary +500 與 hourly_rate +600 合計 1100 > 1000 → 403。

    對齊 manual-adjust 的「合計門檻」設計，不允許拆成兩欄各 < 1000 繞過簽核。
    """
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標E", base_salary=30000, hourly_rate=200)
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(
        f"/api/employees/{target_id}",
        json={
            "base_salary": 30500,
            "hourly_rate": 800,
            "adjustment_reason": "拆欄繞過嘗試",
        },
    )
    assert res.status_code == 403, res.json()


# ── 帶簽核可放行 ─────────────────────────────────────────────────


def test_amount_change_with_reason_and_approve_succeeds(amtguard_client):
    """改 base_salary 帶 reason + ACTIVITY_PAYMENT_APPROVE → 200。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標F", base_salary=30000)
        _make_user(
            s,
            username="boss",
            permissions=(
                Permission.EMPLOYEES_READ
                | Permission.EMPLOYEES_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE
            ),
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "boss").status_code == 200
    res = client.put(
        f"/api/employees/{target_id}",
        json={"base_salary": 38000, "adjustment_reason": "年度調薪 2026"},
    )
    assert res.status_code == 200, res.json()

    with sf() as s:
        emp = s.query(Employee).filter(Employee.id == target_id).first()
        assert emp.base_salary == 38000


def test_small_amount_change_with_reason_no_approve_succeeds(amtguard_client):
    """小額 base_salary 變動（delta=500 ≤ 1000）只要 reason → 200。"""
    client, sf = amtguard_client
    with sf() as s:
        target = _make_employee(s, name="目標G", base_salary=30000)
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(
        f"/api/employees/{target_id}",
        json={"base_salary": 30500, "adjustment_reason": "誤算修正補差"},
    )
    assert res.status_code == 200, res.json()
