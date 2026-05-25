"""家長 data-export endpoint：JSON shape / rate-limit / 413 / IDOR / redacted 家長空回應。"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token

from api.parent_portal._dependencies import get_parent_db
from tests._parent_rls_test_utils import make_sqlite_parent_db_override


@pytest.fixture
def export_client(tmp_path):
    db_path = tmp_path / "parent-export.sqlite"
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


@pytest.fixture(autouse=True)
def _reset_export_limiter():
    """每個測試前清空 rate-limit 計數，避免跨測試洩漏 429。"""
    from api.parent_portal.data_export import _export_limiter

    _export_limiter._timestamps.clear()
    yield
    _export_limiter._timestamps.clear()


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


def _admin_token() -> str:
    return create_access_token(
        {
            "user_id": 9999,
            "employee_id": 1,
            "role": "admin",
            "name": "admin_test",
            "permissions": 2**63 - 1,
            "token_version": 0,
        }
    )


def _seed_parent_with_student(
    session_factory,
    *,
    line_id="U_EXPORT",
    student_name="王大寶",
    lifecycle_status="active",
    terminal_entered_at=None,
):
    """建立 parent_user + student + guardian；回傳 (user, student)。"""
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
            student_id=f"S_{student_name}",
            name=student_name,
            lifecycle_status=lifecycle_status,
            terminal_entered_at=terminal_entered_at,
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


def test_export_returns_json_with_attachment_header(export_client):
    """正常路徑：回 JSON + Content-Disposition: attachment。"""
    client, session_factory = export_client
    user_id, _ = _seed_parent_with_student(session_factory, student_name="王大寶")

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "ivy_data_export" in resp.headers.get("content-disposition", "")

    body = resp.json()
    assert body["exported_by_user_id"] == user_id
    assert body["schema_version"] == 1
    assert len(body["students"]) == 1
    assert body["students"][0]["name"] == "王大寶"


def test_export_rate_limit_429(export_client):
    """第二次 1 小時內呼叫 → 429。"""
    client, session_factory = export_client
    user_id, _ = _seed_parent_with_student(session_factory, line_id="U_RL")

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp1 = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp1.status_code == 200, resp1.text

    resp2 = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp2.status_code == 429


def test_export_returns_empty_for_parent_without_guardians(export_client):
    """沒有任何 Guardian 關係的家長 → students 空 list。"""
    client, session_factory = export_client

    with session_factory() as session:
        user = User(
            username="parent_line_U_NOGD",
            password_hash="!LINE_ONLY",
            role="parent",
            permission_names=[],
            is_active=True,
            line_user_id="U_NOGD",
            token_version=0,
        )
        session.add(user)
        session.commit()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["students"] == []


def test_export_403_for_non_parent_role(export_client):
    """非 parent role 走家長 endpoint → 4xx（401 user 不存在 / 403 角色不符）。

    test DB 中無 user_id=9999 的 admin user，故實際回 401（user 不存在）。
    如果 token 屬於存在的 admin user 則會回 403（角色檢查）。
    兩者都屬於「非家長無法存取」的正確行為。
    """
    client, session_factory = export_client
    # 建一個真實 admin user 在 test DB 中，確保走到角色檢查
    with session_factory() as session:
        admin_user = User(
            username="admin_test_user",
            password_hash="hashed",
            role="admin",
            permission_names=["*"],
            is_active=True,
            token_version=0,
        )
        session.add(admin_user)
        session.commit()
        token = create_access_token(
            {
                "user_id": admin_user.id,
                "employee_id": 1,
                "role": "admin",
                "name": "admin_test_user",
                "permissions": 2**63 - 1,
                "token_version": 0,
            }
        )

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 403


def test_export_includes_all_module_keys(export_client):
    """students[0] 應包含所有模組 keys，即使是空 list。"""
    client, session_factory = export_client
    user_id, _ = _seed_parent_with_student(
        session_factory, student_name="完整寶", line_id="U_COMPLETE"
    )

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    s = body["students"][0]
    for key in [
        "contact_book",
        "attendance",
        "leaves",
        "fees",
        "medications",
        "messages",
        "photos",
        "growth_reports",
    ]:
        assert key in s, f"missing key: {key}"
        assert isinstance(s[key], list), f"{key} should be a list"


def test_export_includes_terminal_students_within_retention(export_client):
    """已畢業但未過保留期的 student 仍應出現在 export。"""
    client, session_factory = export_client
    user_id, _ = _seed_parent_with_student(
        session_factory,
        student_name="畢業寶",
        line_id="U_GRAD",
        lifecycle_status="graduated",
        terminal_entered_at=datetime.now(timezone.utc) - timedelta(days=100),
    )

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [s["name"] for s in body["students"]]
    assert "畢業寶" in names


def test_export_top_level_fields(export_client):
    """回應包含 exported_at / parent / schema_version 等頂層欄位。"""
    client, session_factory = export_client
    user_id, _ = _seed_parent_with_student(
        session_factory, student_name="欄位寶", line_id="U_FIELDS"
    )

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "exported_at" in body
    assert "parent" in body
    assert body["parent"]["display_name"] is not None
    assert body["schema_version"] == 1


def test_export_idor_cross_parent_isolation(export_client):
    """另一個 parent 的 student 不可被本 user 拿到（IDOR 防護）。"""
    client, session_factory = export_client

    # 建第一個 parent（請求發起者）
    user_id, _ = _seed_parent_with_student(
        session_factory, student_name="自己的小孩", line_id="U_IDOR_SELF"
    )

    # 建第二個 parent 及其小孩（應不可見）
    with session_factory() as session:
        other_user = User(
            username="parent_line_U_IDOR_OTHER",
            password_hash="!LINE_ONLY",
            role="parent",
            permission_names=[],
            is_active=True,
            line_user_id="U_IDOR_OTHER",
            token_version=0,
        )
        session.add(other_user)
        session.flush()

        other_student = Student(
            student_id="S_別人家小孩",
            name="別人家小孩",
            lifecycle_status="active",
        )
        session.add(other_student)
        session.flush()

        session.add(
            Guardian(
                student_id=other_student.id,
                user_id=other_user.id,
                name="別人家媽",
                relation="母親",
                is_primary=True,
            )
        )
        session.commit()
        other_student_id = other_student.id

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    student_names = [s["name"] for s in body["students"]]
    assert "別人家小孩" not in student_names
    student_ids = [s["id"] for s in body["students"]]
    assert other_student_id not in student_ids


def test_export_413_when_payload_too_large(export_client, monkeypatch):
    """payload size > _MAX_BYTES 時回 413。"""
    client, session_factory = export_client
    from api.parent_portal import data_export as de

    user_id, _ = _seed_parent_with_student(
        session_factory, student_name="尺寸測試", line_id="U_413"
    )

    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        token = _parent_token(user)

    # 把 _MAX_BYTES 改成 1 byte，任何回應都超
    monkeypatch.setattr(de, "_MAX_BYTES", 1)

    resp = client.get("/api/parent/me/data-export", cookies={"access_token": token})
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "50MB" in detail or "資料量" in detail
