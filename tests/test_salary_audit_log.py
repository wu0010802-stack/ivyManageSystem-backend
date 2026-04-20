"""
P3: 薪資 audit-log 端點與 manual_adjust 結構化 audit_summary 測試。

- GET /api/salaries/{record_id}/audit-log 回傳該筆薪資的 AuditLog 項目
- manual_adjust 寫入 request.state.audit_summary（給 AuditMiddleware 取代通用摘要）
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord, AuditLog
from utils.auth import hash_password


@pytest.fixture
def audit_client(tmp_path):
    db_path = tmp_path / "salary-audit.sqlite"
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

    salary_module.init_salary_services(MagicMock(), MagicMock())

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


def _seed(session_factory):
    with session_factory() as session:
        emp = Employee(
            employee_id="A001",
            name="Audit 測試",
            base_salary=30000,
            employee_type="regular",
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
        user = User(
            employee_id=None,
            username="audit_admin",
            password_hash=hash_password("AuditPass123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        return record.id


def _login(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "audit_admin", "password": "AuditPass123"},
    )
    assert res.status_code == 200


def _insert_audit(
    session_factory, *, entity_type, entity_id, summary, username="audit_admin"
):
    with session_factory() as session:
        session.add(
            AuditLog(
                user_id=None,
                username=username,
                action="UPDATE",
                entity_type=entity_type,
                entity_id=str(entity_id),
                summary=summary,
                ip_address=None,
                created_at=datetime.now(),
            )
        )
        session.commit()


class TestAuditLogEndpoint:
    def test_returns_logs_for_record(self, audit_client):
        client, sf = audit_client
        record_id = _seed(sf)
        _login(client)

        _insert_audit(
            sf,
            entity_type="salary",
            entity_id=record_id,
            summary="手動調整薪資 #1 v1→v2：節慶獎金 1000→1500",
        )
        res = client.get(f"/api/salaries/{record_id}/audit-log")
        assert res.status_code == 200
        body = res.json()
        assert body["record_id"] == record_id
        assert len(body["items"]) == 1
        assert body["items"][0]["summary"].startswith("手動調整薪資")
        assert body["items"][0]["username"] == "audit_admin"
        assert body["items"][0]["action"] == "UPDATE"

    def test_404_for_missing_record(self, audit_client):
        client, sf = audit_client
        _seed(sf)  # 建立 user
        _login(client)
        res = client.get("/api/salaries/99999/audit-log")
        assert res.status_code == 404

    def test_only_salary_entity_returned(self, audit_client):
        """非 salary entity_type 的 AuditLog 不應被此端點回傳"""
        client, sf = audit_client
        record_id = _seed(sf)
        _login(client)

        _insert_audit(sf, entity_type="salary", entity_id=record_id, summary="薪資更新")
        _insert_audit(
            sf, entity_type="employee", entity_id=record_id, summary="員工更新"
        )

        res = client.get(f"/api/salaries/{record_id}/audit-log")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["summary"] == "薪資更新"

    def test_only_matching_record_id_returned(self, audit_client):
        """相同 entity_type 但不同 entity_id 不應被混入"""
        client, sf = audit_client
        record_id = _seed(sf)
        _login(client)

        _insert_audit(sf, entity_type="salary", entity_id=record_id, summary="本筆")
        _insert_audit(sf, entity_type="salary", entity_id=99999, summary="他筆")

        res = client.get(f"/api/salaries/{record_id}/audit-log")
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["summary"] == "本筆"

    def test_ordered_desc_by_created_at(self, audit_client):
        """最新的排第一"""
        client, sf = audit_client
        record_id = _seed(sf)
        _login(client)

        _insert_audit(sf, entity_type="salary", entity_id=record_id, summary="舊")
        _insert_audit(sf, entity_type="salary", entity_id=record_id, summary="新")

        items = client.get(f"/api/salaries/{record_id}/audit-log").json()["items"]
        # 兩筆在同微秒內插入，用 id 順序驗證：新 id 較大且排第一
        assert items[0]["id"] > items[1]["id"]


class TestManualAdjustSetsAuditState:
    """manual_adjust 應把結構化摘要寫入 request.state（供 AuditMiddleware 消費）"""

    def test_audit_summary_set_after_adjust(self, audit_client, monkeypatch):
        client, sf = audit_client
        record_id = _seed(sf)
        _login(client)

        captured = {}

        from starlette.middleware.base import BaseHTTPMiddleware

        class StateCaptureMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                captured["audit_summary"] = getattr(
                    request.state, "audit_summary", None
                )
                captured["audit_entity_id"] = getattr(
                    request.state, "audit_entity_id", None
                )
                return response

        # 重新建立帶 middleware 的 app
        app = FastAPI()
        app.add_middleware(StateCaptureMiddleware)
        app.include_router(auth_router)
        app.include_router(salary_router)

        with TestClient(app) as mw_client:
            _login(mw_client)
            res = mw_client.put(
                f"/api/salaries/{record_id}/manual-adjust",
                json={"festival_bonus": 2500},
            )
            assert res.status_code == 200

        assert captured["audit_entity_id"] == str(record_id)
        assert captured["audit_summary"] is not None
        # 結構化摘要需含：員工編號、年月、版本、欄位名
        assert f"#{record_id}" in captured["audit_summary"]
        assert "v1→v2" in captured["audit_summary"]
        assert "節慶獎金" in captured["audit_summary"]
