"""Tests for /api/parent/measurements endpoints."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.parent_portal.measurements import router as parent_measurements_router
from models.auth import User
from models.database import Base, Classroom, Guardian, Student, StudentMeasurement
from utils.auth import create_access_token


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "parent_measurements.sqlite"
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
    app.include_router(parent_measurements_router, prefix="/api/parent")
    client = TestClient(app)

    with TestingSession() as session:
        parent = User(
            username="parent_a",
            password_hash="$2b$12$dummy",
            role="parent",
            permissions=0,
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

        today = date.today()
        session.add_all(
            [
                StudentMeasurement(
                    student_id=student.id,
                    measured_on=today,
                    height_cm=110,
                    weight_kg=18,
                ),
                StudentMeasurement(
                    student_id=student.id,
                    measured_on=today - timedelta(days=30),
                    height_cm=109,
                    weight_kg=17.5,
                ),
                StudentMeasurement(
                    student_id=student.id,
                    measured_on=today - timedelta(days=60),
                    height_cm=108,
                    weight_kg=17,
                ),
            ]
        )
        session.commit()

        parent_id = parent.id
        student_id = student.id
        other_student_id = other_student.id

    parent_token = create_access_token(
        data={
            "sub": "parent_a",
            "user_id": parent_id,
            "role": "parent",
            "permissions": 0,
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {parent_token}"})

    yield client, TestingSession, student_id, other_student_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_parent_lists_measurements(app_client):
    client, _, student_id, _ = app_client
    resp = client.get(f"/api/parent/measurements?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 3


def test_parent_403_for_other_kid(app_client):
    client, _, _, other_student_id = app_client
    resp = client.get(f"/api/parent/measurements?student_id={other_student_id}")
    assert resp.status_code == 403, resp.text


def test_parent_chart_data_asc_sorted(app_client):
    client, _, student_id, _ = app_client
    resp = client.get(f"/api/parent/measurements/chart-data?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "height" in data
    height = data["height"]
    assert len(height) == 3
    # asc by date
    assert height[0]["x"] < height[1]["x"] < height[2]["x"]
