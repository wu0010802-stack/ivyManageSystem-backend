"""tests/test_data_quality_endpoints.py — Ch2 API endpoints。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.data_quality import router as data_quality_router
from models.database import Base, User
from models.data_quality import DataQualityReport
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "data-quality.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(data_quality_router)

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session, username="dq_admin"):
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=["DATA_QUALITY_READ", "DATA_QUALITY_WRITE"],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_viewer(session, username="dq_viewer"):
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="hr",
        permission_names=["EMPLOYEES_READ"],  # 無 DATA_QUALITY_*
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username):
    rsp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "TempPass123"},
    )
    assert rsp.status_code == 200, rsp.text
    # token 經 httpOnly Cookie 帶；TestClient 自動保留 cookie session
    return rsp


_REPORT_COUNTER = {"n": 0}


def _seed_report(session_factory, **kwargs):
    _REPORT_COUNTER["n"] += 1
    defaults = dict(
        rule_code="x",
        severity="P0",
        entity_type="e",
        entity_id="1",
        summary="s",
        dedup_key=f"d_{_REPORT_COUNTER['n']}",
        status="open",
    )
    defaults.update(kwargs)
    with session_factory() as session:
        row = DataQualityReport(**defaults)
        session.add(row)
        session.commit()
        return row.id


def test_list_reports_requires_permission(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        _create_viewer(session)
        session.commit()
    _login(client, "dq_viewer")

    rsp = client.get("/api/data-quality/reports")
    assert rsp.status_code == 403


def test_list_reports_filters_by_status(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        _create_admin(session)
        session.commit()
    _login(client, "dq_admin")

    _seed_report(sf, dedup_key="d_open_1", status="open")
    _seed_report(sf, dedup_key="d_fixed_1", status="fixed")

    rsp = client.get("/api/data-quality/reports?status=open")
    assert rsp.status_code == 200, rsp.text
    data = rsp.json()
    assert all(r["status"] == "open" for r in data["items"])
    assert data["page"] == 1
    assert "total" in data


def test_ack_marks_status(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        _create_admin(session)
        session.commit()
    _login(client, "dq_admin")
    rid = _seed_report(sf, dedup_key="d_ack")

    rsp = client.post(
        f"/api/data-quality/reports/{rid}/ack",
        json={"note": "看到了"},
    )
    assert rsp.status_code == 200, rsp.text

    with sf() as session:
        row = session.query(DataQualityReport).get(rid)
        assert row.status == "ack"
        assert row.ack_at is not None
        assert row.ack_by is not None


def test_resolve_marks_status_and_note(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        _create_admin(session)
        session.commit()
    _login(client, "dq_admin")
    rid = _seed_report(sf, dedup_key="d_resolve")

    rsp = client.post(
        f"/api/data-quality/reports/{rid}/resolve",
        json={"note": "已修：手動關閉 is_active"},
    )
    assert rsp.status_code == 200, rsp.text

    with sf() as session:
        row = session.query(DataQualityReport).get(rid)
        assert row.status == "fixed"
        assert row.resolved_at is not None
        assert "手動關閉" in row.resolution_note


def test_ignore_marks_status_and_note(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        _create_admin(session)
        session.commit()
    _login(client, "dq_admin")
    rid = _seed_report(sf, dedup_key="d_ignore")

    rsp = client.post(
        f"/api/data-quality/reports/{rid}/ignore",
        json={"note": "業務確認可忽略"},
    )
    assert rsp.status_code == 200, rsp.text

    with sf() as session:
        row = session.query(DataQualityReport).get(rid)
        assert row.status == "ignored"


def test_run_now_returns_summary(client_with_db, monkeypatch):
    client, sf = client_with_db
    with sf() as session:
        _create_admin(session)
        session.commit()
    _login(client, "dq_admin")

    # Mock the scheduler helper to keep test fast / deterministic
    def fake_run():
        return {"detected": 0, "new_open": 0, "ran_at": "2026-05-29T03:00:00+08:00"}

    monkeypatch.setattr(
        "api.data_quality.run_data_quality_once",
        fake_run,
    )

    rsp = client.post("/api/data-quality/run-now")
    assert rsp.status_code == 200, rsp.text
    body = rsp.json()
    assert body["detected"] == 0
    assert "ran_at" in body
