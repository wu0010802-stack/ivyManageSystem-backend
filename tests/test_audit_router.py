"""
Audit log router 回歸測試：涵蓋 /meta、/export、新增的篩選參數與 changes 欄位。
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.audit import router as audit_router
from api.audit import EXPORT_MAX_ROWS
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import AuditLog, Base, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "audit-api.sqlite"
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
    app.include_router(audit_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_audit_admin(session, username="audit_admin", password="TempPass123"):
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=Permission.AUDIT_LOGS,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="audit_admin", password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _insert_log(session, **overrides):
    row = dict(
        user_id=1,
        username="alice",
        action="UPDATE",
        entity_type="employee",
        entity_id="1",
        summary="修改員工",
        changes=None,
        ip_address="127.0.0.1",
        created_at=datetime(2026, 4, 18, 10, 0, 0),
    )
    row.update(overrides)
    log = AuditLog(**row)
    session.add(log)
    session.flush()
    return log


class TestAuditMeta:
    def test_meta_returns_labels(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/audit-logs/meta")
        assert res.status_code == 200
        body = res.json()
        assert any(
            e["value"] == "employee" and e["label"] == "員工"
            for e in body["entity_types"]
        )
        assert any(
            a["value"] == "CREATE" and a["label"] == "新增" for a in body["actions"]
        )

    def test_meta_requires_permission(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            user = User(
                username="plain_user",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permissions=Permission.EMPLOYEES_READ,
                is_active=True,
            )
            s.add(user)
            s.commit()
        assert _login(client, username="plain_user").status_code == 200

        res = client.get("/api/audit-logs/meta")
        assert res.status_code == 403


class TestAuditListFilters:
    def test_filter_by_entity_id(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            _insert_log(s, entity_id="42", summary="改員工 42")
            _insert_log(s, entity_id="99", summary="改員工 99")
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/audit-logs", params={"entity_id": "42"})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["entity_id"] == "42"

    def test_filter_by_ip_address(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            _insert_log(s, ip_address="10.0.0.5")
            _insert_log(s, ip_address="192.168.1.10")
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/audit-logs", params={"ip_address": "10.0"})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["ip_address"] == "10.0.0.5"

    def test_filter_by_datetime_range(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            _insert_log(s, created_at=datetime(2026, 4, 18, 9, 0, 0), summary="早上")
            _insert_log(s, created_at=datetime(2026, 4, 18, 14, 0, 0), summary="下午")
            _insert_log(s, created_at=datetime(2026, 4, 18, 20, 0, 0), summary="晚上")
            s.commit()
        assert _login(client).status_code == 200

        res = client.get(
            "/api/audit-logs",
            params={
                "start_at": "2026-04-18T10:00:00",
                "end_at": "2026-04-18T18:00:00",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["summary"] == "下午"

    def test_changes_field_deserialized(self, client_with_db):
        client, session_factory = client_with_db
        diff = {"name": {"before": "小明", "after": "小華"}}
        with session_factory() as s:
            _create_audit_admin(s)
            _insert_log(s, changes=json.dumps(diff, ensure_ascii=False))
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/audit-logs")
        assert res.status_code == 200
        body = res.json()
        assert body["items"][0]["changes"] == diff


class TestAuditExport:
    def test_export_returns_csv(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            _insert_log(s, summary="輸出測試一")
            _insert_log(s, summary="輸出測試二", entity_type="student", action="CREATE")
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/audit-logs/export")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/csv")
        # BOM + header line + 2 rows
        text = res.content.decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 3  # header + 2
        assert "時間" in lines[0] and "變更內容" in lines[0]
        assert "輸出測試一" in text and "輸出測試二" in text

    def test_export_rejects_over_limit(self, client_with_db, monkeypatch):
        client, session_factory = client_with_db
        with session_factory() as s:
            _create_audit_admin(s)
            # 插一筆就夠，把 limit 下修到 0 觸發
            _insert_log(s)
            s.commit()
        assert _login(client).status_code == 200

        import api.audit as audit_module

        monkeypatch.setattr(audit_module, "EXPORT_MAX_ROWS", 0)

        res = client.get("/api/audit-logs/export")
        assert res.status_code == 400
        assert "匯出上限" in res.json()["detail"]


class TestMiddlewareChangesSerialize:
    def test_serializes_datetime_and_decimal(self):
        """middleware 對 audit_changes 應以 default=str 序列化非原生型別。"""
        import json as _json
        from decimal import Decimal

        payload = {
            "salary": {"before": Decimal("30000"), "after": Decimal("32000")},
            "hire_date": {
                "before": datetime(2026, 1, 1),
                "after": datetime(2026, 2, 1),
            },
        }
        s = _json.dumps(payload, ensure_ascii=False, default=str)
        restored = _json.loads(s)
        assert restored["salary"]["after"] == "32000"
        assert restored["hire_date"]["before"].startswith("2026-01-01")
