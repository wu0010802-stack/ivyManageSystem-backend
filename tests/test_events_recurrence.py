"""events.py CRUD 整合 recurrence_rule 測試。"""

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
from api.events import router as events_router
from models.base import Base
from models.database import User
from utils.auth import hash_password


@pytest.fixture
def events_client(tmp_path):
    db_path = tmp_path / "events_recurrence.sqlite"
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
    app.include_router(events_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, session_factory):
    """全權限 admin。"""
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
        "/api/auth/login",
        json={"username": "admin", "password": "AdminPass1"},
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


# ----- recurrence CRUD tests -----


def test_create_event_with_weekly_rule(events_client):
    """POST 含 recurrence_rule 寫進 DB。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "週會",
            "event_date": "2026-05-05",  # 週二（Python weekday=1）
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-12-29",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recurrence_rule"]["type"] == "weekly"
    assert body["recurrence_rule"]["weekday"] == 1


def test_create_event_with_invalid_rule_returns_422(events_client):
    """weekday mismatch → 422。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "週會",
            "event_date": "2026-05-06",  # 週三 (weekday=2)
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-12-29",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    assert "weekday" in r.json()["detail"].lower()


def test_create_event_runaway_until_rejected(events_client):
    """until > 730 天 → 422。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "永遠",
            "event_date": "2026-01-01",
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 3,
                "until": "2030-01-01",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    assert "730" in r.json()["detail"]


def test_update_event_clear_recurrence(events_client):
    """PUT recurrence_rule=null 清空規則回單次事件。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "x",
            "event_date": "2026-05-05",
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-06-30",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    r = client.put(
        f"/api/events/{eid}",
        json={"recurrence_rule": None},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["recurrence_rule"] is None


# ----- Phase B drag-rescheduled tests -----


def test_drag_patch_updates_single_event_date(events_client):
    """單次事件 PUT 改 event_date → 200，模擬 FC 拖拉改期。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "single",
            "event_date": "2026-05-05",
            "event_type": "meeting",
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    r = client.put(
        f"/api/events/{eid}",
        json={"event_date": "2026-05-12"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["event_date"] == "2026-05-12"


def test_drag_patch_rejects_recurring_event_date_change(events_client):
    """重複事件 PUT 只改 event_date（沒帶 recurrence_rule） → 422。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "recurring",
            "event_date": "2026-05-05",
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-06-30",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    r = client.put(
        f"/api/events/{eid}",
        json={"event_date": "2026-05-12"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422, r.text
    assert "重複事件" in r.json()["detail"]


def test_drag_patch_recurring_with_explicit_rule_change_passes(events_client):
    """重複事件同時改 event_date + recurrence_rule → 200（dialog 編輯路徑）。"""
    client, sf = events_client
    tok = _login_admin(client, sf)
    r = client.post(
        "/api/events",
        json={
            "title": "recurring",
            "event_date": "2026-05-05",
            "event_type": "meeting",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-06-30",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    eid = r.json()["id"]

    # event_date 2026-05-12 (Tuesday, weekday=1) 對齊 rule.weekday=1
    r = client.put(
        f"/api/events/{eid}",
        json={
            "event_date": "2026-05-12",
            "recurrence_rule": {
                "type": "weekly",
                "weekday": 1,
                "until": "2026-07-31",
            },
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["event_date"] == "2026-05-12"
