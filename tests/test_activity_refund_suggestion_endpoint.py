"""GET /api/activity/registrations/{reg_id}/refund-suggestion endpoint 測試。"""

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
from models.database import Base
from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund_suggestion.sqlite"
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


def test_get_refund_suggestion_returns_items(client):
    c, sf = client
    with sf() as s:
        _create_admin(
            s,
            permission_names=[
                "ACTIVITY_READ",
                "ACTIVITY_WRITE",
                "ACTIVITY_PAYMENT_APPROVE",
            ],
        )
        reg = _setup_reg(s, course_price=1500, supply_price=500, paid_amount=2000)
        s.commit()
        reg_id = reg.id

    login = _login(c)
    assert login.status_code == 200

    resp = c.get(f"/api/activity/registrations/{reg_id}/refund-suggestion")
    assert resp.status_code == 200
    data = resp.json()
    assert data["registration_id"] == reg_id
    # 預設 course.sessions=NULL（_setup_reg 沒設）→ fallback 全退
    # supply 用品 suggested=0
    # total_suggested = course amount_due 1500 + supply 0 = 1500
    assert data["total_suggested_amount"] == 1500
    assert data["total_amount_due"] == 2000
    assert len(data["items"]) == 2
    types = sorted(it["type"] for it in data["items"])
    assert types == ["course", "supply"]


def test_get_refund_suggestion_404_not_found(client):
    c, sf = client
    with sf() as s:
        _create_admin(
            s,
            permission_names=[
                "ACTIVITY_READ",
                "ACTIVITY_WRITE",
                "ACTIVITY_PAYMENT_APPROVE",
            ],
        )
        s.commit()

    _login(c)
    resp = c.get("/api/activity/registrations/99999/refund-suggestion")
    assert resp.status_code == 404


def test_get_refund_suggestion_requires_permission(client):
    """無 ACTIVITY_PAYMENT_WRITE 權限應 403。"""
    c, sf = client
    with sf() as s:
        # 只給 READ，沒 WRITE
        _create_admin(s, permission_names=["ACTIVITY_READ"])
        reg = _setup_reg(s)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.get(f"/api/activity/registrations/{reg_id}/refund-suggestion")
    assert resp.status_code == 403
