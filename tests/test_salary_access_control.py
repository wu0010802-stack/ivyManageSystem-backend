"""
回歸測試：薪資查詢 viewer_employee_id 安全漏洞（H1）

Bug 描述：
    GET /api/salaries/records 中，非 admin/hr 角色但帳號缺少 employee_id（NULL）時，
    viewer_employee_id 會是 None，導致「if viewer_employee_id is not None」
    守衛被跳過，該帳號可查看全體員工薪資。

修復方式：
    在 viewer_employee_id 為 None 且 role 不在 FULL_SALARY_ROLES 時，
    主動拋出 403 HTTPException，阻止查詢。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
import api.salary as salary_module
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def salary_client(tmp_path, monkeypatch):
    """建立隔離的 sqlite 測試 app（薪資查詢用）。"""
    db_path = tmp_path / "salary-access-control.sqlite"
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

    # 注入 mock salary engine
    fake_salary_engine = MagicMock()
    fake_insurance_service = MagicMock()
    salary_module.init_salary_services(fake_salary_engine, fake_insurance_service)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, *, username, password, role, permissions, employee_id=None):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestSalaryQueryAccessControl:
    """H1：非 admin/hr 帳號缺少 employee_id 時，薪資查詢應回傳 403。"""

    def test_non_admin_without_employee_id_gets_403(self, salary_client):
        """非特權帳號若缺少 employee_id，不可查詢薪資，應回傳 403。"""
        client, session_factory = salary_client
        with session_factory() as session:
            # teacher 角色，有 SALARY_READ 權限，但無 employee_id（NULL）
            _create_user(
                session,
                username="orphan_teacher",
                password="TeacherPass123",
                role="teacher",
                permissions=int(Permission.SALARY_READ),
                employee_id=None,
            )
            session.commit()

        login_res = _login(client, "orphan_teacher", "TeacherPass123")
        assert login_res.status_code == 200

        res = client.get("/api/salaries/records?year=2026&month=3")
        assert res.status_code == 403
        assert "員工身分" in res.json()["detail"]

    def test_admin_without_employee_id_can_query_all(self, salary_client):
        """admin 帳號即使無 employee_id，仍可查詢全員薪資（特權角色不受限）。"""
        client, session_factory = salary_client
        with session_factory() as session:
            _create_user(
                session,
                username="pure_admin_salary",
                password="AdminPass123",
                role="admin",
                permissions=-1,
                employee_id=None,
            )
            session.commit()

        login_res = _login(client, "pure_admin_salary", "AdminPass123")
        assert login_res.status_code == 200

        res = client.get("/api/salaries/records?year=2026&month=3")
        # 無薪資記錄但不應被 403 擋住，應回傳 200 空列表
        assert res.status_code == 200

    def test_teacher_with_employee_id_sees_only_own_salary(self, salary_client):
        """有 employee_id 的非特權帳號只能查詢自己的薪資（非 403）。"""
        client, session_factory = salary_client
        with session_factory() as session:
            emp = Employee(
                employee_id="T999",
                name="有帳號教師",
                base_salary=36000,
                is_active=True,
            )
            session.add(emp)
            session.flush()
            _create_user(
                session,
                username="linked_teacher",
                password="LinkedPass123",
                role="teacher",
                permissions=int(Permission.SALARY_READ),
                employee_id=emp.id,
            )
            session.commit()

        login_res = _login(client, "linked_teacher", "LinkedPass123")
        assert login_res.status_code == 200

        res = client.get("/api/salaries/records?year=2026&month=3")
        # 有 employee_id，應成功（200），只是查無記錄
        assert res.status_code == 200


class TestManualAdjustNegativeSalaryGuard:
    """V12：手動調整薪資後淨薪資為負數時應回傳 400。"""

    def test_adjust_with_deduction_exceeding_gross_returns_400(self, salary_client):
        """扣款超過應發薪資導致淨薪資為負數時，應回傳 400 而非存入負值。"""
        client, session_factory = salary_client
        with session_factory() as session:
            emp = Employee(
                employee_id="ADJ001",
                name="薪資調整測試員工",
                base_salary=30000,
                is_active=True,
            )
            session.add(emp)
            session.flush()
            # 建立一筆非封存薪資記錄（gross=30000, total_deduction=0, net=30000）
            record = SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=3,
                base_salary=30000,
                gross_salary=30000,
                total_deduction=0,
                net_salary=30000,
                is_finalized=False,
            )
            session.add(record)
            _create_user(
                session,
                username="salary_adjuster",
                password="AdjPass123",
                role="admin",
                permissions=-1,
                employee_id=None,
            )
            session.commit()
            record_id = record.id

        login_res = _login(client, "salary_adjuster", "AdjPass123")
        assert login_res.status_code == 200

        # 將 other_deduction 設為 99999，遠超應發薪資 → 淨薪資應為負數
        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "自動化測試補欄位原因", "other_deduction": 99999},
        )
        assert res.status_code == 400, (
            f"扣款超過薪資應被阻擋（400），但回傳 {res.status_code}: {res.json()}"
        )
        assert "負數" in res.json().get("detail", "")

    def test_adjust_within_gross_succeeds(self, salary_client):
        """扣款未超過應發薪資時，調整應成功（200）。"""
        client, session_factory = salary_client
        with session_factory() as session:
            emp = Employee(
                employee_id="ADJ002",
                name="薪資調整成功員工",
                base_salary=30000,
                is_active=True,
            )
            session.add(emp)
            session.flush()
            record = SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=4,
                base_salary=30000,
                gross_salary=30000,
                total_deduction=0,
                net_salary=30000,
                is_finalized=False,
            )
            session.add(record)
            _create_user(
                session,
                username="salary_adjuster2",
                password="Adj2Pass123",
                role="admin",
                permissions=-1,
                employee_id=None,
            )
            session.commit()
            record_id = record.id

        login_res = _login(client, "salary_adjuster2", "Adj2Pass123")
        assert login_res.status_code == 200

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "自動化測試補欄位原因", "other_deduction": 1000},  # 30000 - 1000 = 29000 > 0
        )
        assert res.status_code == 200
