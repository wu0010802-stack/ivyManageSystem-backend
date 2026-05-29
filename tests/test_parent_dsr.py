"""P0c-2 家長 DSR endpoints：delete-request / correct-request / opt-out / list。

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.2
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from api.parent_portal._dependencies import get_parent_db
from models.consent import CONSENT_SCOPE_PHOTO_PUBLISH
from models.database import Base, Guardian, Student, User
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_REQUEST_TYPE_OPT_OUT,
    DSR_STATUS_PENDING,
    DsrRequest,
)
from tests._parent_rls_test_utils import make_sqlite_parent_db_override
from utils.auth import create_access_token


@pytest.fixture
def dsr_client(tmp_path):
    db_path = tmp_path / "parent-dsr.sqlite"
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


def _seed_parent_with_student(session_factory, *, line_id="U_DSR"):
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
        session.commit()
        return user.id, student.id


# ── delete-request ──


def test_delete_request_creates_pending(dsr_client):
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject_entity_type": "student",
            "subject_entity_id": student_id,
            "reason": "孩子轉學至外縣市學校，要求刪除既有資料",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request_type"] == DSR_REQUEST_TYPE_DELETE
    assert body["status"] == DSR_STATUS_PENDING

    with sf() as session:
        rows = session.query(DsrRequest).all()
        assert len(rows) == 1
        assert rows[0].subject_entity_type == "student"


def test_delete_request_blocks_second_pending(dsr_client):
    """同類同時最多 1 pending。"""
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    payload = {
        "subject_entity_type": "student",
        "subject_entity_id": student_id,
        "reason": "第一次申請理由",
    }
    r1 = client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json={**payload, "reason": "第二次申請理由"},
    )
    assert r2.status_code == 409
    assert "pending delete" in r2.json()["detail"]


def test_delete_request_rejects_unknown_subject_type(dsr_client):
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject_entity_type": "employee",  # 家長不能對 employee 申請
            "subject_entity_id": 999,
            "reason": "abc12345",
        },
    )
    assert r.status_code == 400


def test_delete_request_rejects_short_reason(dsr_client):
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject_entity_type": "student",
            "subject_entity_id": student_id,
            "reason": "x",  # < 5 字
        },
    )
    assert r.status_code == 422


# ── correct-request ──


def test_correct_request_creates_pending(dsr_client):
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/correct-request",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject_entity_type": "student",
            "subject_entity_id": student_id,
            "field_name": "address",
            "new_value": "台北市信義區新地址 100 號",
            "reason": "搬家後新地址",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request_type"] == DSR_REQUEST_TYPE_CORRECT
    assert body["field_name"] == "address"


# ── opt-out ──


def test_opt_out_creates_pending(dsr_client):
    client, sf = dsr_client
    user_id, _ = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_PHOTO_PUBLISH, "reason": "個人隱私考量"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == CONSENT_SCOPE_PHOTO_PUBLISH


def test_opt_out_rejects_unknown_scope(dsr_client):
    client, sf = dsr_client
    user_id, _ = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": "evil_inject"},
    )
    assert r.status_code == 400


# ── list /me/dsr-requests ──


def test_list_dsr_requests_returns_history(dsr_client):
    client, sf = dsr_client
    user_id, student_id = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    # 兩種申請各一筆
    client.post(
        "/api/parent/me/delete-request",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject_entity_type": "student",
            "subject_entity_id": student_id,
            "reason": "刪除申請理由",
        },
    )
    client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_PHOTO_PUBLISH},
    )

    r = client.get(
        "/api/parent/me/dsr-requests",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # 最新在前（opt_out 第二送）
    assert rows[0]["request_type"] == DSR_REQUEST_TYPE_OPT_OUT
    assert rows[1]["request_type"] == DSR_REQUEST_TYPE_DELETE


def test_list_dsr_requests_empty_for_fresh_user(dsr_client):
    client, sf = dsr_client
    user_id, _ = _seed_parent_with_student(sf)
    with sf() as session:
        token = _parent_token(session.query(User).get(user_id))

    r = client.get(
        "/api/parent/me/dsr-requests",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


# ── Unauthorized ──


def test_dsr_endpoints_require_auth(dsr_client):
    client, _ = dsr_client
    for path, payload in [
        (
            "/api/parent/me/delete-request",
            {
                "subject_entity_type": "student",
                "subject_entity_id": 1,
                "reason": "abcde",
            },
        ),
        (
            "/api/parent/me/correct-request",
            {
                "subject_entity_type": "student",
                "subject_entity_id": 1,
                "field_name": "x",
                "new_value": "y",
                "reason": "abcde",
            },
        ),
        ("/api/parent/me/opt-out", {"scope": CONSENT_SCOPE_PHOTO_PUBLISH}),
    ]:
        r = client.post(path, json=payload)
        assert r.status_code in (401, 403)

    r = client.get("/api/parent/me/dsr-requests")
    assert r.status_code in (401, 403)
