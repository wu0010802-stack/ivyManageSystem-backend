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
from datetime import date as _date

from models.contact_book import StudentContactBookEntry
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


def test_parent_photo_urls_use_parent_route_not_admin(app_client):
    """Bug sweep round 4 (2026-05-14) B8：照片牆 URL 必須走家長專用下載路徑。

    舊 bug：reuse api/attachments._attachment_to_dict 產出
    `/api/uploads/portfolio/{key}`，該路由守衛 PORTFOLIO_READ，家長 permissions=0
    沒此 bit → 所有 <img> 一律 403 變破圖。
    修補：local `_parent_attachment_to_dict` 改用 `/api/parent/uploads/...`
    （由 parent_downloads.py 認可家長 owner_type 反查）。
    """
    client, _, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    resp = client.get(f"/api/parent/photos?student_id={ids['student_id']}")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["url"].startswith("/api/parent/uploads/portfolio/"), item["url"]
    # 修補前會是 /api/uploads/portfolio/...，撞到 admin route 403
    assert not item["url"].startswith("/api/uploads/"), (
        "URL 不可走 admin route /api/uploads/，"
        "家長 permissions=0 無 PORTFOLIO_READ bit 會 403"
    )


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


def _add_contact_book_image(session, student_id, classroom_id, *, published, deleted):
    entry = StudentContactBookEntry(
        student_id=student_id,
        classroom_id=classroom_id,
        log_date=_date.today(),
        teacher_note="x",
        published_at=datetime.utcnow() if published else None,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    session.add(entry)
    session.flush()
    img = Attachment(
        owner_type="contact_book_entry",
        owner_id=entry.id,
        storage_key=f"cb/{entry.id}.jpg",
        original_filename="cb.jpg",
        mime_type="image/jpeg",
        size_bytes=1234,
    )
    session.add(img)
    session.commit()
    return entry.id


def _photos_count_for(client, ids, owner_type=None):
    url = f"/api/parent/photos?student_id={ids['student_id']}"
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    if owner_type:
        items = [i for i in items if i.get("owner_type") == owner_type]
    return len(items)


def test_contact_book_draft_attachment_hidden(app_client):
    """REGRESSION: 草稿聯絡簿（published_at=NULL）的照片不可洩漏給家長."""
    client, session_factory, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    with session_factory() as s:
        classroom = s.query(Classroom).first()
        _add_contact_book_image(
            s,
            ids["student_id"],
            classroom.id,
            published=False,
            deleted=False,
        )
    assert _photos_count_for(client, ids, owner_type="contact_book_entry") == 0


def test_contact_book_published_attachment_visible(app_client):
    """已發布聯絡簿照片要對家長可見."""
    client, session_factory, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    with session_factory() as s:
        classroom = s.query(Classroom).first()
        _add_contact_book_image(
            s, ids["student_id"], classroom.id, published=True, deleted=False
        )
    assert _photos_count_for(client, ids, owner_type="contact_book_entry") == 1


def test_contact_book_softdeleted_attachment_hidden(app_client):
    """軟刪聯絡簿照片不可繼續露出."""
    client, session_factory, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    with session_factory() as s:
        classroom = s.query(Classroom).first()
        _add_contact_book_image(
            s, ids["student_id"], classroom.id, published=True, deleted=True
        )
    assert _photos_count_for(client, ids, owner_type="contact_book_entry") == 0


def test_observation_softdeleted_attachment_hidden(app_client):
    """軟刪觀察的照片不可露出（已存在防護，加 explicit 測試守住回歸）."""
    client, session_factory, ids, make_token = app_client
    token = make_token(ids["parent_id"], "parent_a")
    client.headers.update({"Authorization": f"Bearer {token}"})
    with session_factory() as s:
        obs = StudentObservation(
            student_id=ids["student_id"],
            observation_date=_date.today(),
            narrative="d",
            deleted_at=datetime.utcnow(),
        )
        s.add(obs)
        s.flush()
        s.add(
            Attachment(
                owner_type="observation",
                owner_id=obs.id,
                storage_key="obs/d.jpg",
                original_filename="d.jpg",
                mime_type="image/jpeg",
                size_bytes=10,
            )
        )
        s.commit()
    # baseline test 已有 1 張 obs 圖；新加軟刪的不應出現
    body = client.get(f"/api/parent/photos?student_id={ids['student_id']}").json()
    obs_items = [i for i in body["items"] if i.get("owner_type") == "observation"]
    assert (
        len(obs_items) == 1
    ), f"only the non-deleted obs image should show, got {obs_items}"
