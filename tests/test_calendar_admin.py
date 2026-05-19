"""admin_feed endpoint 整合測試。

每個 layer 的 fetch 細節由本檔負責 end-to-end 驗證，
不另寫 unit test（fetcher 是 endpoint 的私有實作）。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.calendar_admin import router as calendar_admin_router
from models.base import Base
from models.database import User
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar_admin_client(tmp_path):
    db_path = tmp_path / "calendar_admin.sqlite"
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
    app.include_router(calendar_admin_router, prefix="/api/calendar")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, session_factory):
    """建一個全權限 admin 並登入回傳 access_token。"""
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
# 邊界 / 參數驗證 (6 tests)
# ---------------------------------------------------------------------------


def test_window_over_90_days_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-01-01", "to": "2026-05-01"},  # 121 天
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    # 區分自定 422（"window exceeds"）與 FastAPI 內建 validation 422
    assert "window" in r.json()["detail"].lower()


def test_to_before_from_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-31", "to": "2026-05-01"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    assert "to must be" in r.json()["detail"].lower()


def test_missing_from_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"to": "2026-05-31"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422


def test_unauthenticated_returns_401(calendar_admin_client):
    client, _ = calendar_admin_client
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31"},
    )
    assert r.status_code == 401


def test_empty_window_returns_empty_items(calendar_admin_client):
    """無任何資料的 window 回 200 + items=[]"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "2099-01-01"
    assert body["to"] == "2099-01-07"
    assert body["items"] == []


def test_unknown_layer_ignored(calendar_admin_client):
    """`?layers=foo` 不報錯，當作沒指定有效 layer。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07", "layers": "foo,bar"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# event layer (Task 4)
# ---------------------------------------------------------------------------

from models.event import SchoolEvent


def test_event_layer_basic(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="家長會",
                event_date=date(2026, 5, 20),
                event_type="meeting",
                is_active=True,
                requires_acknowledgment=False,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["layer"] == "event"
    assert it["title"] == "家長會"
    assert it["start"] == "2026-05-20"
    assert it["end"] == "2026-05-20"
    assert it["color"] == "#10b981"
    assert it["link"] == f"/calendar?eventId={it['id']}"


def test_event_multi_day_uses_end_date(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="校外教學",
                event_date=date(2026, 5, 20),
                end_date=date(2026, 5, 22),
                event_type="activity",
                is_active=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["start"] == "2026-05-20"
    assert items[0]["end"] == "2026-05-22"


def test_event_requires_ack_uses_ack_color(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="家長簽閱通知",
                event_date=date(2026, 5, 20),
                is_active=True,
                requires_acknowledgment=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.json()["items"][0]["color"] == "#ef4444"


def test_event_inactive_excluded(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="已停用",
                event_date=date(2026, 5, 20),
                is_active=False,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.json()["items"] == []
