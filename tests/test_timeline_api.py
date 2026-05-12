"""Integration tests for /api/students/{id}/timeline router (P2)."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portfolio.milestones import router as milestones_router
from api.portfolio.timeline import router as timeline_router
from models.auth import User
from models.database import Base, Classroom, Student


@pytest.fixture(scope="function")
def app_client(monkeypatch):
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
    app.include_router(timeline_router)
    client = TestClient(app)

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
            lifecycle_status="active",
        )
        session.add_all([admin, classroom, student])
        session.commit()

    from utils.auth import create_access_token

    token = create_access_token(
        data={
            "sub": "admin",
            "user_id": 1,
            "role": "admin",
            "permissions": -1,
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client, TestingSession
    Base.metadata.drop_all(engine)


def test_timeline_empty_returns_empty_items(app_client):
    client, _ = app_client
    resp = client.get("/api/students/1/timeline")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


def test_timeline_includes_milestones(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "birthday",
            "achieved_on": date.today().isoformat(),
            "title": "5 歲生日",
            "icon": "🎂",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = client.get("/api/students/1/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["type"] == "milestone"
    assert item["title"] == "5 歲生日"
    assert item["icon"] == "🎂"


def test_timeline_filter_by_types(app_client):
    client, _ = app_client
    client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "custom",
            "achieved_on": date.today().isoformat(),
            "title": "A",
        },
    )
    resp = client.get("/api/students/1/timeline?types=milestone")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(item["type"] == "milestone" for item in items)


def test_timeline_date_range_filter(app_client):
    client, _ = app_client
    today = date.today()
    client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "custom",
            "achieved_on": (today - timedelta(days=30)).isoformat(),
            "title": "舊里程碑",
        },
    )
    client.post(
        "/api/students/1/milestones",
        json={
            "milestone_type": "custom",
            "achieved_on": today.isoformat(),
            "title": "新里程碑",
        },
    )
    since = (today - timedelta(days=7)).isoformat()
    resp = client.get(f"/api/students/1/timeline?since={since}")
    titles = [i["title"] for i in resp.json()["items"]]
    assert "新里程碑" in titles
    assert "舊里程碑" not in titles


def test_timeline_includes_measurements(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.database import StudentMeasurement

        session.add(
            StudentMeasurement(
                student_id=1,
                measured_on=date.today(),
                height_cm=110.5,
                weight_kg=18.2,
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    assert any(it["type"] == "measurement" for it in items)


def test_timeline_includes_observations(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.database import StudentObservation

        session.add(
            StudentObservation(
                student_id=1,
                observation_date=date.today(),
                narrative="測試觀察",
                domain="認知",
                is_highlight=True,
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    obs_items = [it for it in items if it["type"] == "observation"]
    assert len(obs_items) == 1
    assert obs_items[0]["is_highlight"] is True
