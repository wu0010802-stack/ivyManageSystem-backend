"""tests/test_gov_moe_subsidies.py — 特教加給/助理鐘點費 endpoint tests."""

import os
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.database import User
from models.gov_moe import (
    SpecialEducationSubsidy,
)  # noqa: F401 — registers table on Base
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures (copied verbatim from test_gov_moe_certificates.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def gov_moe_client(tmp_path):
    db_path = tmp_path / "gov_moe.sqlite"
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
    app.include_router(gov_moe_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Auth helper (copied verbatim from Sub-system C tests)
# ---------------------------------------------------------------------------


def _login_admin(client, session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "AdminPass1"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


# ---------------------------------------------------------------------------
# B1 test: list returns empty list
# ---------------------------------------------------------------------------


def test_subsidies_list_returns_empty(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/gov-moe/subsidies",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json() == []
