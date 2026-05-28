"""P0c-1 家長 consent endpoint：POST /me/consent / GET /me/consents / GET /policies/current。

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §4.2
"""

from __future__ import annotations

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
from api.parent_portal import parent_router
from api.parent_portal._dependencies import get_parent_db
from models.consent import (
    CONSENT_SCOPE_LINE_PUSH,
    CONSENT_SCOPE_PHOTO_PUBLISH,
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.database import Base, Guardian, Student, User
from tests._parent_rls_test_utils import make_sqlite_parent_db_override
from utils.auth import create_access_token


@pytest.fixture
def consent_client(tmp_path):
    db_path = tmp_path / "parent-consent.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(parent_router)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


def _seed_parent_and_policy(session_factory, *, line_id="U_CONSENT"):
    with session_factory() as session:
        user = User(
            username=f"parent_line_{line_id}",
            password_hash="!LINE_ONLY",
            role="parent",
            permission_names=[],
            is_active=True,
            line_user_id=line_id,
            token_version=0,
        )
        session.add(user)
        session.flush()

        student = Student(
            student_id=f"S_{line_id}",
            name="王小寶",
            lifecycle_status="active",
        )
        session.add(student)
        session.flush()

        guardian = Guardian(
            student_id=student.id,
            user_id=user.id,
            name="王媽媽",
            relation="母親",
            is_primary=True,
        )
        session.add(guardian)

        policy = PolicyVersion(
            version="2026.1",
            effective_at=datetime(2026, 1, 1),
            document_path="policies/2026.1.pdf",
            summary="2026 年首版隱私權政策",
        )
        session.add(policy)
        session.commit()
        return user.id, policy.id


# ── POST /me/consent ──


def test_post_consent_writes_log(consent_client):
    client, sf = consent_client
    user_id, policy_id = _seed_parent_and_policy(sf)

    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    r = client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_SERVICE_ESSENTIAL,
            "consented": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == CONSENT_SCOPE_SERVICE_ESSENTIAL
    assert body["consented"] is True

    with sf() as session:
        logs = session.query(ParentConsentLog).all()
        assert len(logs) == 1
        assert logs[0].user_id == user_id
        assert logs[0].scope == CONSENT_SCOPE_SERVICE_ESSENTIAL
        assert logs[0].consented is True


def test_post_consent_withdraw_writes_separate_log(consent_client):
    """同 scope 撤回 = 新 log（consented=False），不修改既有 log。"""
    client, sf = consent_client
    user_id, policy_id = _seed_parent_and_policy(sf)
    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    # 先同意
    client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_PHOTO_PUBLISH,
            "consented": True,
        },
    )
    # 後撤回
    r = client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_PHOTO_PUBLISH,
            "consented": False,
            "note": "我不希望子女照片公開",
        },
    )
    assert r.status_code == 200
    assert r.json()["consented"] is False

    with sf() as session:
        logs = (
            session.query(ParentConsentLog)
            .filter(ParentConsentLog.scope == CONSENT_SCOPE_PHOTO_PUBLISH)
            .order_by(ParentConsentLog.id.asc())
            .all()
        )
        assert len(logs) == 2
        assert logs[0].consented is True
        assert logs[1].consented is False
        assert logs[1].note == "我不希望子女照片公開"


def test_post_consent_unknown_scope_rejected(consent_client):
    client, sf = consent_client
    user_id, policy_id = _seed_parent_and_policy(sf)
    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    r = client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": "evil_scope_inject",
            "consented": True,
        },
    )
    assert r.status_code == 400
    assert "未知 scope" in r.json()["detail"]


def test_post_consent_invalid_policy_id_rejected(consent_client):
    client, sf = consent_client
    user_id, _policy_id = _seed_parent_and_policy(sf)
    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    r = client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": 99999,
            "scope": CONSENT_SCOPE_SERVICE_ESSENTIAL,
            "consented": True,
        },
    )
    assert r.status_code == 400
    assert "policy_version_id 不存在" in r.json()["detail"]


# ── GET /me/consents ──


def test_get_consents_latest_status_per_scope(consent_client):
    """current_status 每 scope 取最新一筆。"""
    client, sf = consent_client
    user_id, policy_id = _seed_parent_and_policy(sf)
    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    # service_essential: 同意 → 撤回（最新狀態 = False）
    client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_SERVICE_ESSENTIAL,
            "consented": True,
        },
    )
    client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_SERVICE_ESSENTIAL,
            "consented": False,
        },
    )
    # line_push: 只同意一次
    client.post(
        "/api/parent/me/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "policy_version_id": policy_id,
            "scope": CONSENT_SCOPE_LINE_PUSH,
            "consented": True,
        },
    )

    r = client.get(
        "/api/parent/me/consents", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    status_by_scope = {s["scope"]: s for s in body["current_status"]}

    assert status_by_scope[CONSENT_SCOPE_SERVICE_ESSENTIAL]["consented"] is False
    assert status_by_scope[CONSENT_SCOPE_LINE_PUSH]["consented"] is True
    # 未簽過的 scope
    assert status_by_scope[CONSENT_SCOPE_PHOTO_PUBLISH]["consented"] is None

    # history 共 3 筆，最新在前
    assert len(body["history"]) == 3
    # 第一筆 history = 最後寫入（line_push consented=True）
    assert body["history"][0]["scope"] == CONSENT_SCOPE_LINE_PUSH


def test_get_consents_empty_for_fresh_user(consent_client):
    """新使用者：current_status 全部 None，history 為空。"""
    client, sf = consent_client
    user_id, _policy_id = _seed_parent_and_policy(sf)
    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    r = client.get(
        "/api/parent/me/consents", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["history"] == []
    for s in body["current_status"]:
        assert s["consented"] is None


# ── GET /policies/current ──


def test_get_current_policy_returns_latest_effective(consent_client):
    client, sf = consent_client
    user_id, policy_id = _seed_parent_and_policy(sf)

    # 加 v2 future policy（effective_at 未到）+ v1.5 已生效更新版
    with sf() as session:
        future = PolicyVersion(
            version="2026.99",
            effective_at=datetime(2030, 1, 1),
            document_path="policies/2026.99.pdf",
        )
        latest = PolicyVersion(
            version="2026.2",
            effective_at=datetime(2026, 3, 1),
            document_path="policies/2026.2.pdf",
            summary="新增照片公開 scope",
        )
        session.add_all([future, latest])
        session.commit()

    with sf() as session:
        user = session.query(User).get(user_id)
        token = _parent_token(user)

    r = client.get(
        "/api/parent/policies/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "2026.2"  # 最新生效中（future 還未到）
    assert body["summary"] == "新增照片公開 scope"


def test_get_current_policy_404_when_no_policy(consent_client):
    """空 policy table → 404。"""
    client, sf = consent_client

    with sf() as session:
        user = User(
            username="parent_line_X",
            password_hash="!LINE_ONLY",
            role="parent",
            permission_names=[],
            is_active=True,
            line_user_id="X",
            token_version=0,
        )
        session.add(user)
        session.commit()
        token = _parent_token(user)

    r = client.get(
        "/api/parent/policies/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


# ── Unauthorized ──


def test_post_consent_requires_auth(consent_client):
    client, _ = consent_client
    r = client.post(
        "/api/parent/me/consent",
        json={
            "policy_version_id": 1,
            "scope": CONSENT_SCOPE_SERVICE_ESSENTIAL,
            "consented": True,
        },
    )
    assert r.status_code in (401, 403)


def test_get_consents_requires_auth(consent_client):
    client, _ = consent_client
    r = client.get("/api/parent/me/consents")
    assert r.status_code in (401, 403)
