"""Tests for /api/parent/milestones endpoints."""

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
from api.auth import _account_failures, _ip_attempts
from api.parent_portal.milestones import router as parent_milestones_router
from models.auth import User
from models.database import Base, Classroom, Guardian, Student, StudentMilestone
from utils.auth import create_access_token


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "parent_milestones.sqlite"
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
    app.include_router(parent_milestones_router, prefix="/api/parent")

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        TestingSession
    )
    client = TestClient(app)

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
        )
        other_student = Student(
            student_id="S099",
            name="李小華",
            classroom_id=classroom.id,
        )
        session.add_all([student, other_student])
        session.flush()

        guardian = Guardian(
            user_id=parent.id,
            student_id=student.id,
            name="王家長",
            relation="父親",
            is_primary=True,
        )
        session.add(guardian)

        milestone = StudentMilestone(
            student_id=student.id,
            milestone_type="birthday",
            achieved_on=date(2026, 5, 1),
            title="五歲生日",
        )
        session.add(milestone)
        session.commit()

        parent_id = parent.id
        student_id = student.id
        other_student_id = other_student.id
        milestone_id = milestone.id

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

    yield client, TestingSession, student_id, other_student_id, milestone_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_parent_lists_own_kid_milestone(app_client):
    client, _, student_id, _, milestone_id = app_client
    resp = client.get(f"/api/parent/milestones?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == milestone_id
    assert items[0]["title"] == "五歲生日"


def test_parent_403_for_other_kid(app_client):
    client, _, _, other_student_id, _ = app_client
    resp = client.get(f"/api/parent/milestones?student_id={other_student_id}")
    assert resp.status_code == 403, resp.text


def test_parent_react_sets_reaction(app_client):
    client, session_factory, student_id, _, milestone_id = app_client
    resp = client.post(
        f"/api/parent/milestones/{milestone_id}/react?student_id={student_id}",
        json={"reaction": "love"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["parent_reaction"] == "love"
    # react also auto-acknowledges
    assert data["parent_acknowledged_at"] is not None


def test_parent_react_invalid_value(app_client):
    client, _, student_id, _, milestone_id = app_client
    resp = client.post(
        f"/api/parent/milestones/{milestone_id}/react?student_id={student_id}",
        json={"reaction": "dislike"},
    )
    assert resp.status_code == 422, resp.text


def test_parent_acknowledge_idempotent(app_client):
    client, _, student_id, _, milestone_id = app_client
    # first ack
    resp1 = client.post(
        f"/api/parent/milestones/{milestone_id}/acknowledge?student_id={student_id}"
    )
    assert resp1.status_code == 200, resp1.text
    first_ack_time = resp1.json()["parent_acknowledged_at"]
    assert first_ack_time is not None

    # second ack must not change the timestamp
    resp2 = client.post(
        f"/api/parent/milestones/{milestone_id}/acknowledge?student_id={student_id}"
    )
    assert resp2.status_code == 200, resp2.text
    second_ack_time = resp2.json()["parent_acknowledged_at"]
    assert second_ack_time == first_ack_time


def test_parent_react_rate_limit_blocks_spam(app_client):
    """F-V6-07：parent_react 10/60s/IP；第 11 次同 IP react 在 60 秒內應回 429。"""
    from api.parent_portal.milestones import _react_limiter

    # limiter 是 module-level singleton；其他測試可能累計過 count
    _react_limiter._timestamps.clear()

    client, _, student_id, _, milestone_id = app_client
    url = f"/api/parent/milestones/{milestone_id}/react?student_id={student_id}"

    # 10 次都應成功
    for i in range(10):
        resp = client.post(url, json={"reaction": "love"})
        assert resp.status_code == 200, f"call {i + 1}: {resp.text}"

    # 第 11 次應被 limiter 擋下
    resp = client.post(url, json={"reaction": "celebrate"})
    assert resp.status_code == 429, resp.text


def test_second_guardian_ack_does_not_overwrite_first(app_client):
    """F-V6-04：first-ack-wins semantic — 同學生兩位 guardian（爸/媽）依序 ack
    時，第二位的 ack 不應覆蓋 parent_acknowledged_at 與 parent_acknowledged_by。
    SQLite 環境下 with_for_update 為 no-op；本測試驗 sequential semantic（避免
    race fix 退化為「都寫」）。
    """
    client, session_factory, student_id, _, milestone_id = app_client

    # 取 fixture 已建的 guardian id（父親）
    with session_factory() as session:
        first_g = (
            session.query(Guardian).filter(Guardian.student_id == student_id).first()
        )
        first_guardian_id = first_g.id

        # 建第二位 user + guardian（母親）
        second_user = User(
            username="parent_mother",
            password_hash="$2b$12$dummy",
            role="parent",
            permission_names=[],
            is_active=True,
            token_version=0,
        )
        session.add(second_user)
        session.flush()
        second_g = Guardian(
            user_id=second_user.id,
            student_id=student_id,
            name="王媽媽",
            relation="母親",
            is_primary=False,
        )
        session.add(second_g)
        session.commit()
        second_user_id = second_user.id
        second_guardian_id = second_g.id

    # 父親先 ack
    resp1 = client.post(
        f"/api/parent/milestones/{milestone_id}/acknowledge?student_id={student_id}"
    )
    assert resp1.status_code == 200, resp1.text
    first_ack_at = resp1.json()["parent_acknowledged_at"]
    assert first_ack_at is not None

    # 母親隨後 ack：換 token
    mother_token = create_access_token(
        data={
            "sub": "parent_mother",
            "user_id": second_user_id,
            "role": "parent",
            "permission_names": [],
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {mother_token}"})
    resp2 = client.post(
        f"/api/parent/milestones/{milestone_id}/acknowledge?student_id={student_id}"
    )
    assert resp2.status_code == 200, resp2.text

    # parent_acknowledged_at 不變；attribution 仍指父親
    with session_factory() as session:
        m = session.query(StudentMilestone).filter_by(id=milestone_id).first()
        assert m.parent_acknowledged_at.isoformat() == first_ack_at
        assert m.parent_acknowledged_by == first_guardian_id
        assert m.parent_acknowledged_by != second_guardian_id
