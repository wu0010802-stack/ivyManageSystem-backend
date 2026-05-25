"""Integration tests for /api/parent/timeline endpoint."""

from __future__ import annotations

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
from api.parent_portal.timeline import router as parent_timeline_router
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "parent_timeline.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    TestingSession = sessionmaker(bind=engine, autoflush=False)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = TestingSession
    Base.metadata.create_all(engine)

    app = FastAPI()
    # Parent timeline router mounted under /api/parent prefix
    app.include_router(parent_timeline_router, prefix="/api/parent")

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        TestingSession
    )
    client = TestClient(app)

    # 建立 parent user + classroom + student + guardian binding
    with TestingSession() as session:
        parent = User(
            username="parent_a",
            password_hash="$2b$12$dummy",
            role="parent",
            permission_names=[],
            is_active=True,
            token_version=0,
        )
        session.add(parent)
        session.flush()

        classroom = Classroom(name="兔兔班", is_active=True)
        session.add(classroom)
        session.flush()

        student = Student(
            student_id="S001",
            name="王小明",
            classroom_id=classroom.id,
            is_active=True,
        )
        # 另一位學生（不屬此家長）用於 403 測試
        other_student = Student(
            student_id="S099",
            name="李小華",
            classroom_id=classroom.id,
            is_active=True,
        )
        session.add_all([student, other_student])
        session.flush()

        # Guardian binding: parent 擁有 student S001
        guardian = Guardian(
            user_id=parent.id,
            student_id=student.id,
            name="王家長",
            relation="父親",
            is_primary=True,
        )
        session.add(guardian)
        session.commit()

        parent_id = parent.id
        student_id = student.id
        other_student_id = other_student.id

    parent_token = create_access_token(
        data={
            "sub": "parent_a",
            "user_id": parent_id,
            "role": "parent",
            "permission_names": [],
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {parent_token}"})

    yield client, TestingSession, student_id, other_student_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_parent_timeline_empty(app_client):
    client, _, student_id, _ = app_client
    resp = client.get(f"/api/parent/timeline?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["stats"]["total_items"] == 0


def test_parent_cannot_see_other_kid(app_client):
    client, _, _, other_student_id = app_client
    resp = client.get(f"/api/parent/timeline?student_id={other_student_id}")
    assert resp.status_code == 403, resp.text


def test_parent_sees_milestone(app_client):
    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        from models.database import StudentMilestone

        session.add(
            StudentMilestone(
                student_id=student_id,
                milestone_type="birthday",
                achieved_on=date.today(),
                title="5 歲生日",
                icon="🎂",
                source_type="manual",
            )
        )
        session.commit()
    resp = client.get(f"/api/parent/timeline?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "milestone"


def test_parent_timeline_hides_contact_book_drafts_and_soft_deleted(app_client):
    """round 5 P1：timeline 不得吐老師草稿（published_at IS NULL）或軟刪
    （deleted_at IS NOT NULL）的聯絡簿。"""
    from datetime import datetime, timedelta

    from models.contact_book import StudentContactBookEntry
    from models.database import Student

    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        classroom_id = session.get(Student, student_id).classroom_id
        today = date.today()
        session.add_all(
            [
                # 已發布 — 應出現
                StudentContactBookEntry(
                    student_id=student_id,
                    classroom_id=classroom_id,
                    log_date=today,
                    teacher_note="今天表現很好",
                    published_at=datetime.utcnow(),
                ),
                # 草稿 — 不應出現
                StudentContactBookEntry(
                    student_id=student_id,
                    classroom_id=classroom_id,
                    log_date=today - timedelta(days=1),
                    teacher_note="老師還在寫，未發布",
                    published_at=None,
                ),
                # 已發布但軟刪 — 不應出現
                StudentContactBookEntry(
                    student_id=student_id,
                    classroom_id=classroom_id,
                    log_date=today - timedelta(days=2),
                    teacher_note="已撤回",
                    published_at=datetime.utcnow(),
                    deleted_at=datetime.utcnow(),
                ),
            ]
        )
        session.commit()
    resp = client.get(
        f"/api/parent/timeline?student_id={student_id}&types=contact_book"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1, items
    # 只剩已發布那筆
    assert "今天表現很好" in items[0]["summary"]
