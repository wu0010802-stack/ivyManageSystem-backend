"""Tests for /api/students/{id}/attachments aggregator."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.portfolio.student_attachments import router as student_attachments_router
from models.auth import User
from models.database import (
    Attachment,
    AuditLog,
    Base,
    Classroom,
    Student,
    StudentObservation,
)


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "student_attachments.sqlite"
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
    app.include_router(student_attachments_router)
    client = TestClient(app)

    with TestingSession() as session:
        admin = User(
            username="admin",
            password_hash="$2b$12$dummy",
            role="admin",
            permission_names=["*"],
            is_active=True,
            token_version=0,
        )
        session.add(admin)
        session.flush()

        classroom = Classroom(name="兔兔班", is_active=True)
        session.add(classroom)
        session.flush()

        student = Student(
            student_id="S001",
            name="王小明",
            classroom_id=classroom.id,
        )
        session.add(student)
        session.flush()

        # One observation owning two attachments: one image, one PDF
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
            storage_key="x/y/z.jpg",
            original_filename="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1000,
        )
        att_pdf = Attachment(
            owner_type="observation",
            owner_id=obs.id,
            storage_key="x/y/w.pdf",
            original_filename="doc.pdf",
            mime_type="application/pdf",
            size_bytes=2000,
        )
        session.add_all([att_img, att_pdf])
        session.commit()
        ids = {
            "admin_id": admin.id,
            "student_id": student.id,
            "obs_id": obs.id,
        }

    from utils.auth import create_access_token

    token = create_access_token(
        data={
            "sub": "admin",
            "user_id": ids["admin_id"],
            "role": "admin",
            "permission_names": ["*"],
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {token}"})

    yield client, TestingSession, ids

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_lists_image_attachments_only(app_client):
    client, _, ids = app_client
    resp = client.get(f"/api/students/{ids['student_id']}/attachments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    # 只有 1 張 image，PDF 應被排除
    assert body["total"] == 1
    assert len(items) == 1
    assert items[0]["mime_type"] == "image/jpeg"


def test_filter_by_owner_type(app_client):
    client, _, ids = app_client
    resp = client.get(
        f"/api/students/{ids['student_id']}/attachments?owner_type=observation"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert all(it["owner_type"] == "observation" for it in items)


def test_unsupported_owner_type_422(app_client):
    client, _, ids = app_client
    resp = client.get(
        f"/api/students/{ids['student_id']}/attachments?owner_type=message"
    )
    assert resp.status_code == 422


def test_until_includes_same_day_uploads(app_client):
    """REGRESSION: until=YYYY-MM-DD 必須涵蓋當天 23:59:59 上傳的圖.

    Bug: Attachment.created_at <= date 會被 cast 成 <= date 00:00:00，當天的
    timestamp（如 14:30:00）會被排除（agent P2 #8）。
    """
    client, session_factory, ids = app_client
    today = datetime.now().date()
    with session_factory() as s:
        obs = s.query(StudentObservation).first()
        # 加一張今天 14:30 上傳的圖
        s.add(
            Attachment(
                owner_type="observation",
                owner_id=obs.id,
                storage_key="t/today.jpg",
                original_filename="today.jpg",
                mime_type="image/jpeg",
                size_bytes=500,
                created_at=datetime.combine(today, datetime.min.time()).replace(
                    hour=14, minute=30
                ),
            )
        )
        s.commit()

    resp = client.get(
        f"/api/students/{ids['student_id']}/attachments?until={today.isoformat()}"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    same_day = [i for i in items if i.get("original_filename") == "today.jpg"]
    assert (
        len(same_day) == 1
    ), f"當天上傳的圖必須出現在 until=今天 的查詢中, got items={items}"


def test_attachments_get_writes_read_audit(app_client):
    """F-V6-03：跨模組附件聚合 GET 必須留下 AuditLog action=READ 痕跡。"""
    import time

    client, session_factory, ids = app_client
    resp = client.get(
        f"/api/students/{ids['student_id']}/attachments?owner_type=observation"
    )
    assert resp.status_code == 200, resp.text

    # write_explicit_audit 是 fire-and-forget；等背景寫入落地
    time.sleep(0.1)
    with session_factory() as session:
        rows = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "READ",
                AuditLog.entity_type == "student",
                AuditLog.entity_id == str(ids["student_id"]),
            )
            .all()
        )
    assert any(
        "portfolio 跨模組附件聚合" in (r.summary or "") for r in rows
    ), f"未找到 portfolio attachments READ audit；rows={[(r.entity_id, r.summary) for r in rows]}"
