"""Tests for /api/students/{id}/milestones router (P1 of growth profile)."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portfolio.milestones import router as milestones_router
from models.database import Base, Classroom, Student, User
from models.classroom import LIFECYCLE_ACTIVE


@pytest.fixture(scope="function")
def app_client(tmp_path, monkeypatch):
    _account_failures.clear()
    _ip_attempts.clear()

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestingSession = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(base_module, "_engine", engine)
    monkeypatch.setattr(base_module, "_SessionFactory", TestingSession)

    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(milestones_router)
    client = TestClient(app)

    # 建立 admin User + 一個班級 + 一個學生
    with TestingSession() as session:
        admin = User(
            id=1,
            username="admin",
            password_hash="$2b$12$dummy",
            role="admin",
            permissions=-1,
            is_active=True,
            token_version=0,
        )
        classroom = Classroom(id=1, name="兔兔班", is_active=True)
        student = Student(
            id=1,
            student_id="S001",
            name="王小明",
            classroom_id=1,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        session.add_all([admin, classroom, student])
        session.commit()

    token = _make_test_token(role="admin", user_id=1, username="admin")
    client.headers.update({"Authorization": f"Bearer {token}"})

    yield client, TestingSession

    Base.metadata.drop_all(engine)


def _make_test_token(role: str, user_id: int, username: str) -> str:
    """產生測試用 JWT；與 test_student_measurements 同 pattern。"""
    from utils.auth import create_access_token

    return create_access_token(
        data={
            "sub": username,
            "user_id": user_id,
            "role": role,
            "permissions": -1,  # -1 = 全部權限
            "token_version": 0,
        }
    )


def test_create_milestone_success(app_client):
    """POST 回傳 201，milestone_type='birthday'，source_type 預設 'manual'。"""
    client, _ = app_client
    resp = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "birthday",
            "achieved_on": date.today().isoformat(),
            "title": "王小明三歲生日",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["student_id"] == 1
    assert body["milestone_type"] == "birthday"
    assert body["source_type"] == "manual"


def test_create_milestone_invalid_type(app_client):
    """POST 使用不在 MILESTONE_TYPES 的 milestone_type 回傳 422。"""
    client, _ = app_client
    resp = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "nope",
            "achieved_on": date.today().isoformat(),
            "title": "不合法的類型",
        },
    )
    assert resp.status_code == 422


def test_create_milestone_rejects_future_date(app_client):
    """POST 使用未來日期 achieved_on 回傳 422。"""
    client, _ = app_client
    resp = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "birthday",
            "achieved_on": (date.today() + timedelta(days=1)).isoformat(),
            "title": "未來生日",
        },
    )
    assert resp.status_code == 422


def test_list_milestones_excludes_soft_deleted(app_client):
    """POST 2 筆、DELETE 1 筆，GET list 只回傳未刪除的那筆。"""
    client, _ = app_client
    today = date.today().isoformat()

    r1 = client.post(
        "/api/students/1/milestones",
        json={"milestone_type": "birthday", "achieved_on": today, "title": "生日1"},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/students/1/milestones",
        json={"milestone_type": "graduation", "achieved_on": today, "title": "畢業2"},
    )
    assert r2.status_code == 201, r2.text

    del_resp = client.delete(f"/api/students/1/milestones/{r1.json()['id']}")
    assert del_resp.status_code == 204

    list_resp = client.get("/api/students/1/milestones")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    ids = [item["id"] for item in items]
    assert r1.json()["id"] not in ids
    assert r2.json()["id"] in ids
    assert len(items) == 1


def test_list_milestones_filter_by_type(app_client):
    """POST birthday + custom，GET with milestone_type=birthday 只回傳 1 筆。"""
    client, _ = app_client
    today = date.today().isoformat()

    client.post(
        "/api/students/1/milestones",
        json={"milestone_type": "birthday", "achieved_on": today, "title": "生日"},
    )
    client.post(
        "/api/students/1/milestones",
        json={"milestone_type": "custom", "achieved_on": today, "title": "自訂"},
    )

    resp = client.get("/api/students/1/milestones?milestone_type=birthday")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["milestone_type"] == "birthday"


def test_update_milestone(app_client):
    """PATCH 更新 title + description；其他欄位保持不變。"""
    client, _ = app_client
    created = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "birthday",
            "achieved_on": date.today().isoformat(),
            "title": "原始標題",
        },
    ).json()

    resp = client.patch(
        f"/api/students/1/milestones/{created['id']}",
        json={"title": "更新後標題", "description": "新增描述"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "更新後標題"
    assert body["description"] == "新增描述"
    assert body["milestone_type"] == "birthday"  # 未動到


def test_delete_milestone_is_soft(app_client):
    """DELETE 回傳 204；直接查 DB 仍存在該筆且 deleted_at IS NOT NULL。"""
    client, session_factory = app_client
    created = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "birthday",
            "achieved_on": date.today().isoformat(),
            "title": "要軟刪除的里程碑",
        },
    ).json()

    resp = client.delete(f"/api/students/1/milestones/{created['id']}")
    assert resp.status_code == 204

    # 直接查 DB 確認軟刪除（row 存在，deleted_at != NULL）
    with session_factory() as session:
        from models.database import StudentMilestone

        row = (
            session.query(StudentMilestone)
            .filter(StudentMilestone.id == created["id"])
            .first()
        )
        assert row is not None, "軟刪除後 row 應仍存在"
        assert row.deleted_at is not None, "deleted_at 應被設定"
