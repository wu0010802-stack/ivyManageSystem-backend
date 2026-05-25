"""tests/test_gov_moe_disability_documents.py — 教育部障礙文件 CRUD 整合測試。

涵蓋：
- 空列表查詢
- 建立文件（admin 可，teacher 不可）
- 更新文件
- 刪除文件
- 以 student_id 過濾列表
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
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.database import Student, User
from models.gov_moe import (
    StudentDisabilityDocument,
)  # noqa: F401 — registers table on Base
from utils.auth import hash_password
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# Fixtures
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


def _seed_admin(session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permission_names=["*"],
                is_active=True,
            )
        )
        s.commit()


def _seed_teacher(session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="teacher",
                password_hash=hash_password("TeacherPass1"),
                role="teacher",
                permission_names=["DASHBOARD"],
                is_active=True,
            )
        )
        s.commit()


def _seed_student(session_factory):
    with session_factory() as s:
        student = Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 1, 1),
            is_active=True,
        )
        s.add(student)
        s.commit()
        s.refresh(student)
        return student.id


def _login(client, username: str, password: str):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    token = res.json().get("access_token") or res.cookies.get("access_token")
    return token


@pytest.fixture
def authed_client_admin(gov_moe_client):
    client, sf = gov_moe_client
    _seed_admin(sf)
    token = _login(client, "admin", "AdminPass1")
    if token:
        client.headers.update({"Authorization": f"Bearer {token}"})
    return client, sf


@pytest.fixture
def authed_client_teacher(authed_client_admin):
    """Teacher client sharing the same DB as authed_client_admin."""
    client, sf = authed_client_admin
    _seed_teacher(sf)
    token = _login(client, "teacher", "TeacherPass1")
    # Return a separate TestClient instance with teacher token so admin
    # headers on the shared client don't interfere.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC
    from api.gov_moe import router as _gov_moe_router

    app2 = FastAPI()
    from api.auth import router as _auth_router

    app2.include_router(_auth_router)
    app2.include_router(_gov_moe_router, prefix="/api")
    tc = _TC(app2)
    if token:
        tc.headers.update({"Authorization": f"Bearer {token}"})
    return tc, sf


@pytest.fixture
def sample_student(authed_client_admin):
    client, sf = authed_client_admin
    student_id = _seed_student(sf)

    class _Stub:
        id = student_id

    return _Stub()


@pytest.fixture
def sample_disability_doc(authed_client_admin, sample_student):
    client, sf = authed_client_admin
    res = client.post(
        "/api/gov-moe/disability-documents",
        json={
            "student_id": sample_student.id,
            "doc_type": "鑑定證明",
            "file_path": "/uploads/seed.pdf",
            "issued_date": "2026-01-01",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()

    class _Stub:
        id = data["id"]
        student_id = data["student_id"]

    return _Stub()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_disability_docs_empty(authed_client_admin, sample_student):
    client, _ = authed_client_admin
    res = client.get(
        f"/api/gov-moe/disability-documents?student_id={sample_student.id}"
    )
    assert res.status_code == 200
    assert res.json() == []


def test_create_disability_doc(authed_client_admin, sample_student):
    client, _ = authed_client_admin
    payload = {
        "student_id": sample_student.id,
        "doc_type": "鑑定證明",
        "file_path": "/uploads/test.pdf",
        "issued_date": "2026-01-15",
        "expiry_date": "2027-12-31",
        "notes": "測試備註",
    }
    res = client.post("/api/gov-moe/disability-documents", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["doc_type"] == "鑑定證明"
    assert data["student_id"] == sample_student.id


def test_create_requires_permission(authed_client_teacher, sample_student):
    client, _ = authed_client_teacher
    payload = {
        "student_id": sample_student.id,
        "doc_type": "鑑定證明",
        "file_path": "/uploads/x.pdf",
    }
    res = client.post("/api/gov-moe/disability-documents", json=payload)
    assert res.status_code == 403


def test_update_disability_doc(authed_client_admin, sample_disability_doc):
    client, _ = authed_client_admin
    res = client.put(
        f"/api/gov-moe/disability-documents/{sample_disability_doc.id}",
        json={"notes": "已更新"},
    )
    assert res.status_code == 200
    assert res.json()["notes"] == "已更新"


def test_delete_disability_doc(authed_client_admin, sample_disability_doc):
    client, _ = authed_client_admin
    res = client.delete(f"/api/gov-moe/disability-documents/{sample_disability_doc.id}")
    assert res.status_code == 204


def test_list_filters_by_student(authed_client_admin, sample_disability_doc):
    client, _ = authed_client_admin
    student_id = sample_disability_doc.student_id
    res = client.get(f"/api/gov-moe/disability-documents?student_id={student_id}")
    assert res.status_code == 200
    docs = res.json()
    assert len(docs) >= 1
    assert all(d["student_id"] == student_id for d in docs)


# ---------------------------------------------------------------------------
# P1-6/7 security regression tests
# ---------------------------------------------------------------------------


def test_create_rejects_file_path_traversal(authed_client_admin, sample_student):
    """P1-7：file_path 含 .. 必須拒絕（pydantic 422）。"""
    client, _ = authed_client_admin
    for bad in ("..//etc/passwd", "uploads/../../etc/passwd", "x\0y"):
        res = client.post(
            "/api/gov-moe/disability-documents",
            json={
                "student_id": sample_student.id,
                "doc_type": "鑑定證明",
                "file_path": bad,
            },
        )
        assert res.status_code == 422, f"path={bad!r} 應 422，實際 {res.status_code}"


def test_view_only_user_cannot_write(gov_moe_client, sample_student):
    """P1-6：僅持 GOV_REPORTS_VIEW（無 EXPORT）的 user 不可 POST/PUT/DELETE。"""
    client, sf = gov_moe_client
    with sf() as s:
        s.add(
            User(
                username="viewer",
                password_hash=hash_password("ViewPass1"),
                role="hr",
                permission_names=["GOV_REPORTS_VIEW"],
                is_active=True,
            )
        )
        s.commit()
    token = _login(client, "viewer", "ViewPass1")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # POST 應 403
    res = client.post(
        "/api/gov-moe/disability-documents",
        json={
            "student_id": sample_student.id,
            "doc_type": "鑑定證明",
            "file_path": "uploads/x.pdf",
        },
        headers=headers,
    )
    assert res.status_code == 403, res.text

    # 讀取仍可（VIEW 權限就是給讀的）
    res = client.get(
        f"/api/gov-moe/disability-documents?student_id={sample_student.id}",
        headers=headers,
    )
    assert res.status_code == 200
