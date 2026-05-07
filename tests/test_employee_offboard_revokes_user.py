"""驗證員工辦離職時撤對應 User 帳號（is_active=False + token_version+1）。

威脅：原本 POST /api/employees/{id}/offboard 只設 Employee.is_active=False
與 resign_date，未動 User。離職員工 cookie 仍有效，可繼續呼叫
/api/exports/employee-attendance 下載自己出勤月報、合約金額（若曾為
admin/hr/supervisor）。

修補：離職日 <= 今天時連帶把 User.is_active 改 False、token_version+1。
通知期（resign_date > today）保留 User active，等當日 cron 自動處理。

Refs: 邏輯漏洞 audit 2026-05-07 P1。
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
from api.employees import router as employees_router
from models.database import Base, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "offboard_revoke.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_emp(session, *, name, emp_no="E001"):
    e = Employee(employee_id=emp_no, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _seed_user(session, *, username, role, employee_id, permissions, token_version=0):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
        employee_id=employee_id,
        token_version=token_version,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


ADMIN_PERMS = -1
HR_PERMS = int(Permission.EMPLOYEES_WRITE) | int(Permission.EMPLOYEES_READ)


class TestOffboardRevokesUser:
    def test_offboard_today_revokes_user(self, client):
        """離職日 = 今天 → User.is_active=False，token_version 從 0 升到 1。"""
        c, sf = client
        with sf() as s:
            # admin caller（不可被自己撤）
            _seed_user(
                s,
                username="admin1",
                role="admin",
                employee_id=None,
                permissions=ADMIN_PERMS,
            )
            target_emp = _seed_emp(s, name="離職員工", emp_no="E_LEAVE")
            target_user = _seed_user(
                s,
                username="leaving",
                role="hr",
                employee_id=target_emp.id,
                permissions=HR_PERMS,
                token_version=0,
            )
            s.commit()
            target_emp_id = target_emp.id
            target_user_id = target_user.id

        assert _login(c, "admin1").status_code == 200

        res = c.post(
            f"/api/employees/{target_emp_id}/offboard",
            json={
                "resign_date": date.today().isoformat(),
                "resign_reason": "個人因素",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["is_active"] is False
        assert body["user_account_revoked"] is True

        with sf() as s:
            user = s.get(User, target_user_id)
            assert user.is_active is False
            assert user.token_version == 1

    def test_offboard_future_resign_keeps_user_active(self, client):
        """通知期（resign_date > today）→ User 保持 active，等 cron 處理。"""
        c, sf = client
        future = date.today() + timedelta(days=30)
        with sf() as s:
            _seed_user(
                s,
                username="admin2",
                role="admin",
                employee_id=None,
                permissions=ADMIN_PERMS,
            )
            target_emp = _seed_emp(s, name="通知期員工", emp_no="E_NOTICE")
            target_user = _seed_user(
                s,
                username="notice_user",
                role="teacher",
                employee_id=target_emp.id,
                permissions=int(Permission.EMPLOYEES_READ),
            )
            s.commit()
            target_emp_id = target_emp.id
            target_user_id = target_user.id

        assert _login(c, "admin2").status_code == 200

        res = c.post(
            f"/api/employees/{target_emp_id}/offboard",
            json={
                "resign_date": future.isoformat(),
                "resign_reason": "預先通知",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        # 通知期：emp 仍 active，user 也 active
        assert body["is_active"] is True
        assert body["user_account_revoked"] is False

        with sf() as s:
            user = s.get(User, target_user_id)
            assert user.is_active is True
            assert user.token_version == 0  # 未升

    def test_offboard_employee_without_user_account(self, client):
        """員工沒對應 User 帳號時不該爆。"""
        c, sf = client
        with sf() as s:
            _seed_user(
                s,
                username="admin3",
                role="admin",
                employee_id=None,
                permissions=ADMIN_PERMS,
            )
            target_emp = _seed_emp(s, name="無帳號員工", emp_no="E_NO_USER")
            s.commit()
            target_emp_id = target_emp.id

        assert _login(c, "admin3").status_code == 200

        res = c.post(
            f"/api/employees/{target_emp_id}/offboard",
            json={
                "resign_date": date.today().isoformat(),
                "resign_reason": "正常離職",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["is_active"] is False
        assert body["user_account_revoked"] is False

    def test_offboard_already_inactive_user_no_double_bump(self, client):
        """User 已 inactive 不再被處理（filter is_active=True）。"""
        c, sf = client
        with sf() as s:
            _seed_user(
                s,
                username="admin4",
                role="admin",
                employee_id=None,
                permissions=ADMIN_PERMS,
            )
            target_emp = _seed_emp(s, name="重複離職", emp_no="E_DUP")
            target_user = _seed_user(
                s,
                username="already_off",
                role="teacher",
                employee_id=target_emp.id,
                permissions=int(Permission.EMPLOYEES_READ),
                token_version=5,
            )
            target_user.is_active = False
            s.flush()
            s.commit()
            target_emp_id = target_emp.id
            target_user_id = target_user.id

        assert _login(c, "admin4").status_code == 200

        res = c.post(
            f"/api/employees/{target_emp_id}/offboard",
            json={
                "resign_date": date.today().isoformat(),
                "resign_reason": "重複登錄",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["user_account_revoked"] is False

        with sf() as s:
            user = s.get(User, target_user_id)
            assert user.token_version == 5  # 未升
