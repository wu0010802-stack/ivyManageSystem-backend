"""Tests for /api/students/{id}/milestones/auto-detect."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portfolio.auto_milestone import router as auto_milestone_router
from api.portfolio.milestones import router as milestones_router
from models.database import Base, Classroom, Student, User
from utils.auth import create_access_token, hash_password

# ── Fixture ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def app_client():
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

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = TestingSession

    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(milestones_router)
    app.include_router(auto_milestone_router)

    with TestClient(app) as client:
        with TestingSession() as session:
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="admin",
                permissions=-1,
                is_active=True,
                token_version=0,
            )
            session.add(admin)
            session.flush()
            admin_id = admin.id

            classroom = Classroom(id=1, name="兔兔班", is_active=True)
            session.add(classroom)
            session.flush()

            student = Student(
                id=1,
                student_id="S001",
                name="王小明",
                classroom_id=1,
                lifecycle_status="active",
                birthday=date(2022, 3, 5),
                enrollment_date=date(2024, 9, 1),
            )
            session.add(student)
            session.commit()

        # Generate token directly (avoid login endpoint complexity)
        token = create_access_token(
            data={
                "sub": "admin",
                "user_id": admin_id,
                "role": "admin",
                "permissions": -1,
                "token_version": 0,
            }
        )
        client.headers.update({"Authorization": f"Bearer {token}"})

        yield client, TestingSession

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ── Tests ─────────────────────────────────────────────────────────────────


def test_auto_detect_creates_first_day(app_client):
    client, _ = app_client
    resp = client.post("/api/students/1/milestones/auto-detect")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created_count"] >= 1
    # 確認 first_day 出現在清單
    list_resp = client.get("/api/students/1/milestones?milestone_type=first_day")
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["source_type"] == "auto_enrollment"


def test_auto_detect_creates_birthdays(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/milestones/auto-detect",
        json={"reference_date": "2026-05-01"},
    )
    assert resp.status_code == 200, resp.text
    # 學生 birthday=2022/3/5, ref=2026/5/1 → 應建 1/2/3/4 歲生日
    list_resp = client.get("/api/students/1/milestones?milestone_type=birthday")
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()["items"]
    assert len(items) == 4  # 1 歲、2 歲、3 歲、4 歲


def test_auto_detect_is_idempotent(app_client):
    client, _ = app_client
    r1 = client.post("/api/students/1/milestones/auto-detect")
    assert r1.status_code == 200, r1.text
    first_count = r1.json()["created_count"]
    r2 = client.post("/api/students/1/milestones/auto-detect")
    assert r2.status_code == 200, r2.text
    # 第二次新建 0 筆
    assert r2.json()["created_count"] == 0
    assert r2.json()["skipped_existing"] == first_count
