"""驗證 PUT/DELETE /api/insurance/brackets 寫入 request.state.audit_changes，
且 utils/audit.py ENTITY_PATTERNS 把 /api/insurance 對到 entity_type=insurance_bracket。

威脅：勞健保級距表 DB 化後（2026-05-07）只在端點落 logger.warning，AuditMiddleware
不認 /api/insurance 路徑就完全不寫 audit_logs，事後溯源無法用 audit-logs 篩選
「誰在什麼時候改了哪一年的級距」。

Refs: 資安掃描 2026-05-07 P0。
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
from api.insurance import router as insurance_router
from models.database import Base, InsuranceBracket, User
from utils.audit import ENTITY_LABELS, ENTITY_PATTERNS, _parse_entity_type
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def insurance_client(tmp_path):
    db_path = tmp_path / "insurance_audit.sqlite"
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
    app.include_router(insurance_router)

    captured = {"audit_changes": None, "audit_entity_id": None}

    @app.middleware("http")
    async def capture_audit(request, call_next):
        response = await call_next(request)
        captured["audit_changes"] = getattr(request.state, "audit_changes", None)
        captured["audit_entity_id"] = getattr(request.state, "audit_entity_id", None)
        return response

    with TestClient(app) as client:
        yield client, session_factory, captured

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


_FINANCE_PERMS = ["SALARY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


def _create_admin(session):
    u = User(
        username="admin_ins",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=_FINANCE_PERMS,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client):
    return client.post(
        "/api/auth/login",
        json={"username": "admin_ins", "password": "Passw0rd!"},
    )


_BRACKET_ROW = {
    "amount": 30300,
    "labor_employee": 612,
    "labor_employer": 2127,
    "health_employee": 470,
    "health_employer": 1494,
    "pension": 1818,
}


class TestAuditPattern:
    def test_insurance_path_resolves_to_insurance_bracket(self):
        """ENTITY_PATTERNS 必須能把 /api/insurance/brackets 路徑映射到
        entity_type=insurance_bracket，否則 AuditMiddleware 不會落 audit_logs。"""
        assert _parse_entity_type("/api/insurance/brackets") == "insurance_bracket"
        assert _parse_entity_type("/api/insurance/brackets/123") == "insurance_bracket"
        assert "insurance_bracket" in ENTITY_LABELS


class TestPutBracketsAuditChanges:
    def test_put_sets_audit_changes(self, insurance_client):
        client, sf, captured = insurance_client
        with sf() as s:
            _create_admin(s)
            s.commit()

        assert _login(client).status_code == 200
        res = client.put(
            "/api/insurance/brackets",
            json={
                "effective_year": 2026,
                "replace_existing": True,
                "brackets": [_BRACKET_ROW],
                "reason": "115年4月公告新分級表整批上傳",
            },
        )
        assert res.status_code == 200, res.text

        ac = captured["audit_changes"]
        assert ac is not None, "PUT /api/insurance/brackets 必須設 audit_changes"
        assert ac["effective_year"] == 2026
        assert ac["upserted"] == 1
        assert ac["replaced_existing"] is True
        # reason 必須落 audit（事後溯源用）
        assert "115年4月公告新分級表整批上傳" in ac["reason"]
        # entity_id 用 effective_year 作 key（級距表沒有單一 row id 表達整年）
        assert captured["audit_entity_id"] == 2026


class TestDeleteBracketAuditChanges:
    def test_delete_sets_audit_changes(self, insurance_client):
        client, sf, captured = insurance_client
        with sf() as s:
            _create_admin(s)
            row = InsuranceBracket(effective_year=2026, **_BRACKET_ROW)
            s.add(row)
            s.commit()
            bracket_id = row.id

        assert _login(client).status_code == 200
        res = client.request(
            "DELETE",
            f"/api/insurance/brackets/{bracket_id}",
            json={"reason": "誤新增 30300 級距，需立刻刪除修正"},
        )
        assert res.status_code == 200, res.text

        ac = captured["audit_changes"]
        assert ac is not None, "DELETE 必須設 audit_changes"
        assert ac["effective_year"] == 2026
        assert ac["amount"] == 30300
        assert "誤新增" in ac["reason"]
        assert captured["audit_entity_id"] == bracket_id
