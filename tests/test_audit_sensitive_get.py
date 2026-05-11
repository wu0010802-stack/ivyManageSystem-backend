"""驗證白名單敏感 GET 端點呼叫時會寫 audit_logs（action=READ）。

Refs: docs/superpowers/specs/2026-05-11-audit-coverage-gap-design.md §4
"""

import os
import sys
import time
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import AuditLog, Base, User, Employee
from utils.auth import hash_password, create_access_token
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    from api.auth import router as auth_router
    from api.employees import router as employees_router
    from api.employees_docs import router as employees_docs_router

    db_path = tmp_path / "sensitive-get.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    # Build admin + employee
    session = session_factory()
    try:
        emp = Employee(employee_id="T99", name="王小明", is_active=True)
        session.add(emp)
        session.commit()
        emp_id = emp.id

        admin = User(
            username="admin",
            password_hash=hash_password("Admin1234"),
            role="admin",
            is_active=True,
            permissions=-1,
            employee_id=emp_id,
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
    finally:
        session.close()

    token = create_access_token(
        {
            "user_id": admin_id,
            "employee_id": emp_id,
            "role": "admin",
            "name": "王小明",
            "permissions": -1,
            "token_version": 0,
        }
    )

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)
    app.include_router(employees_docs_router)

    client = TestClient(app)
    client.cookies.set("access_token", token)
    yield client, session_factory, emp_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def _get_read_audits(session_factory, entity_type=None):
    time.sleep(0.05)
    session = session_factory()
    try:
        q = session.query(AuditLog).filter(AuditLog.action == "READ")
        if entity_type:
            q = q.filter(AuditLog.entity_type == entity_type)
        rows = q.order_by(AuditLog.id).all()
    finally:
        session.close()
    return rows


class TestEmployeeSensitiveGetAudit:
    def test_employee_detail_get_creates_audit(self, client_with_db):
        client, sf, emp_id = client_with_db
        res = client.get(f"/api/employees/{emp_id}")
        # 200 or 404 both acceptable; we just need the audit row
        rows = _get_read_audits(sf, entity_type="employee")
        assert any(
            r.entity_id == str(emp_id) for r in rows
        ), f"未找到 emp_id={emp_id} 的 employee READ audit；rows={[(r.entity_id, r.summary) for r in rows]}"

    def test_employee_list_does_not_audit(self, client_with_db):
        client, sf, _ = client_with_db
        client.get("/api/employees")
        rows = _get_read_audits(sf, entity_type="employee")
        list_rows = [r for r in rows if r.entity_id is None]
        assert len(list_rows) == 0, f"列表不應寫 READ audit；找到 {len(list_rows)} 筆"

    def test_employee_educations_get_creates_audit(self, client_with_db):
        client, sf, emp_id = client_with_db
        client.get(f"/api/employees/{emp_id}/educations")
        rows = _get_read_audits(sf, entity_type="employee")
        assert any(
            r.entity_id == str(emp_id) and "學歷" in (r.summary or "") for r in rows
        ), f"未找到學歷 READ audit；rows={[(r.entity_id, r.summary) for r in rows]}"

    def test_employee_certificates_get_creates_audit(self, client_with_db):
        client, sf, emp_id = client_with_db
        client.get(f"/api/employees/{emp_id}/certificates")
        rows = _get_read_audits(sf, entity_type="employee")
        assert any(
            r.entity_id == str(emp_id) and "證照" in (r.summary or "") for r in rows
        )

    def test_employee_contracts_get_creates_audit(self, client_with_db):
        client, sf, emp_id = client_with_db
        client.get(f"/api/employees/{emp_id}/contracts")
        rows = _get_read_audits(sf, entity_type="employee")
        assert any(
            r.entity_id == str(emp_id) and "合約" in (r.summary or "") for r in rows
        )
