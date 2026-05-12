"""Tests for GET /api/parent/photos — 家長端照片牆."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.parent_portal.photos import router as parent_photos_router
from models.auth import User
from models.database import (
    Attachment,
    Base,
    Classroom,
    Guardian,
    Student,
    StudentObservation,
)
from utils.auth import create_access_token


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "parent_photos.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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
    app.include_router(parent_photos_router, prefix="/api/parent")
    client = TestClient(app)

    with TestingSession() as session:
        # Parent user
        parent = User(
            username="parent_a",
            password_hash="$2b$12$dummy",
            role="parent",
            permissions=0,
            is_active=True,
            token_version=0,
        )
        # Unrelated parent
        other_parent = User(
            username="parent_b",
            password_hash="$2b$12$dummy",
            role="parent",
            permissions=0,
            is_active=True,
            token_version=0,
        )
        session.add_all([parent, other_parent])
        session.flush()

        classroom = Classroom(name="兔兔班", is_active=True)
        session.add(classroom)
        session.flush()

        student = Student(
            student_id="S001",
            name="王小明",
            classroom_id=classroom.id,
        )
        other_student = Student(
            student_id="S099",
            name="李小華",
            classroom_id=classroom.id,
        )
        session.add_all([student, other_student])
        session.flush()

        # Guardian binding: parent_a → student only
        guardian = Guardian(
            user_id=parent.id,
            student_id=student.id,
            name="王家長",
            relation="父親",
        )
        session.add(guardian)

        # Observation + attachments for own student
        obs = StudentObservation(
            student_id=student.id,
            observation_date=datetime.now().date(),
            narrative="測試觀察",
        )
        session.add(obs)
        session.flush()

        att_img = Attachment(
            owner_type="observation",
            owner_id=obs.id,
            storage_key="photos/a.jpg",
            original_filename="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1500,
        )
        att_pdf = Attachment(
            owner_type="observation",
            owner_id=obs.id,
            storage_key="docs/b.pdf",
            original_filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=3000,
        )
        session.add_all([att_img, att_pdf])
        session.commit()

        ids = {
            "parent_id": parent.id,
            "other_parent_id": other_parent.id,
            "student_id": student.id,
            "other_student_id": other_student.id,
        }

    def _make_token(user_id: int, username: str) -> str:
        return create_access_token(
            data={
                "sub": username,
                "user_id": user_id,
                "role": "parent",
                "permissions": 0,
                "token_version": 0,
            }
        )

    yield client, TestingSession, ids, _make_token

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_parent_lists_own_photos(app_client):
    client, _, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    resp = client.get(f"/api/parent/photos?student_id={ids['student_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only image; PDF excluded
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["mime_type"] == "image/jpeg"


def test_parent_403_for_other_kid(app_client):
    client, _, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    resp = client.get(f"/api/parent/photos?student_id={ids['other_student_id']}")
    assert resp.status_code == 403


def test_parent_empty_when_no_attachments(app_client):
    """other_student has no attachments at all — should return empty list."""
    client, session_factory, ids, make_token = app_client
    # Bind other_parent → other_student so they can access
    with session_factory() as session:
        guardian = Guardian(
            user_id=ids["other_parent_id"],
            student_id=ids["other_student_id"],
            name="李家長",
            relation="母親",
        )
        session.add(guardian)
        session.commit()

    token = make_token(ids["other_parent_id"], "parent_b")
    client.headers.update({"Authorization": f"Bearer {token}"})
    resp = client.get(f"/api/parent/photos?student_id={ids['other_student_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_pdf_not_included_in_photos(app_client):
    """Explicitly verify PDF attachments are filtered out."""
    client, _, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    resp = client.get(f"/api/parent/photos?student_id={ids['student_id']}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    for item in items:
        assert item["mime_type"].startswith("image/")
