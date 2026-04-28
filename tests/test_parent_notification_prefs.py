"""家長通知偏好（Phase 6）。

驗證：
- GET 預設全 enabled（無 row）
- PUT batch upsert
- unknown event_type 拒絕
- is_pref_enabled helper（缺 row = True；row enabled=false → False）
- should_push_to_parent 連動：pref disabled 時 gate 擋下
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from api.parent_portal.notifications import is_pref_enabled
from models.database import (
    Base,
    ParentNotificationPreference,
    User,
)
from models.parent_notification import PARENT_NOTIFICATION_EVENT_TYPES
from services.line_service import LineService
from utils.auth import create_access_token


@pytest.fixture
def pref_client(tmp_path):
    db_path = tmp_path / "prefs.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=db_engine)

    old = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(parent_router)
    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def _make_parent_user(sf, *, with_line=True, follow=True):
    with sf() as session:
        u = User(
            username="p",
            password_hash="!",
            role="parent",
            permissions=0,
            is_active=True,
            line_user_id="U001" if with_line else None,
            line_follow_confirmed_at=datetime.now() if follow else None,
            token_version=0,
        )
        session.add(u)
        session.commit()
        return u.id


def _token(uid: int) -> str:
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": None,
            "role": "parent",
            "name": "p",
            "permissions": 0,
            "token_version": 0,
        }
    )


# ════════════════════════════════════════════════════════════════════════
# GET / PUT 端點
# ════════════════════════════════════════════════════════════════════════


class TestPreferencesEndpoint:
    def test_get_defaults_all_enabled(self, pref_client):
        client, sf = pref_client
        uid = _make_parent_user(sf)
        resp = client.get(
            "/api/parent/notifications/preferences",
            cookies={"access_token": _token(uid)},
        )
        assert resp.status_code == 200
        prefs = resp.json()["prefs"]
        for ev in PARENT_NOTIFICATION_EVENT_TYPES:
            assert prefs[ev] is True

    def test_put_disable_message_received(self, pref_client):
        client, sf = pref_client
        uid = _make_parent_user(sf)
        resp = client.put(
            "/api/parent/notifications/preferences",
            json={"prefs": {"message_received": False}},
            cookies={"access_token": _token(uid)},
        )
        assert resp.status_code == 200
        assert resp.json()["prefs"]["message_received"] is False
        # 其他保持 True
        assert resp.json()["prefs"]["announcement"] is True

        # GET 應反映
        get_resp = client.get(
            "/api/parent/notifications/preferences",
            cookies={"access_token": _token(uid)},
        )
        assert get_resp.json()["prefs"]["message_received"] is False

    def test_put_re_enable_overrides(self, pref_client):
        client, sf = pref_client
        uid = _make_parent_user(sf)
        client.put(
            "/api/parent/notifications/preferences",
            json={"prefs": {"announcement": False}},
            cookies={"access_token": _token(uid)},
        )
        client.put(
            "/api/parent/notifications/preferences",
            json={"prefs": {"announcement": True}},
            cookies={"access_token": _token(uid)},
        )
        with sf() as session:
            row = (
                session.query(ParentNotificationPreference)
                .filter(
                    ParentNotificationPreference.user_id == uid,
                    ParentNotificationPreference.event_type == "announcement",
                )
                .first()
            )
            assert row is not None
            assert row.enabled is True

    def test_put_unknown_event_type_rejected(self, pref_client):
        client, sf = pref_client
        uid = _make_parent_user(sf)
        resp = client.put(
            "/api/parent/notifications/preferences",
            json={"prefs": {"foobar": False}},
            cookies={"access_token": _token(uid)},
        )
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════════
# is_pref_enabled helper
# ════════════════════════════════════════════════════════════════════════


class TestIsPrefEnabled:
    def test_missing_row_defaults_true(self, pref_client):
        _, sf = pref_client
        uid = _make_parent_user(sf)
        with sf() as session:
            assert (
                is_pref_enabled(session, user_id=uid, event_type="message_received")
                is True
            )

    def test_disabled_row_returns_false(self, pref_client):
        _, sf = pref_client
        uid = _make_parent_user(sf)
        with sf() as session:
            session.add(
                ParentNotificationPreference(
                    user_id=uid,
                    event_type="message_received",
                    channel="line",
                    enabled=False,
                )
            )
            session.commit()
            assert (
                is_pref_enabled(session, user_id=uid, event_type="message_received")
                is False
            )


# ════════════════════════════════════════════════════════════════════════
# 連通到 should_push_to_parent gate
# ════════════════════════════════════════════════════════════════════════


class TestPushGateRespectsPref:
    def test_disabled_pref_blocks_push_gate(self, pref_client):
        _, sf = pref_client
        uid = _make_parent_user(sf)
        # Disable message_received
        with sf() as session:
            session.add(
                ParentNotificationPreference(
                    user_id=uid,
                    event_type="message_received",
                    channel="line",
                    enabled=False,
                )
            )
            session.commit()

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True)
        with sf() as session:
            line_id = svc.should_push_to_parent(
                session, user_id=uid, event_type="message_received"
            )
            assert line_id is None  # gate 擋住

    def test_enabled_other_event_passes(self, pref_client):
        _, sf = pref_client
        uid = _make_parent_user(sf)
        with sf() as session:
            session.add(
                ParentNotificationPreference(
                    user_id=uid,
                    event_type="message_received",
                    channel="line",
                    enabled=False,
                )
            )
            session.commit()

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True)
        with sf() as session:
            # announcement 沒 row → enabled 預設 → 通過
            line_id = svc.should_push_to_parent(
                session, user_id=uid, event_type="announcement"
            )
            assert line_id == "U001"
