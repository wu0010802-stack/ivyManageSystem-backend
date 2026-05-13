"""Tests for /api/students/{id}/measurements router (P1 of growth profile)."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
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
from api.portfolio.measurements import router as measurements_router
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
    app.include_router(measurements_router)
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

    # in-memory SQLite + StaticPool：dispose 即釋放連線、DB 隨之消失。
    # 不可用 drop_all：appraisal_cycles 等表存在 FK cycle，SQLite 不支援
    # ALTER TABLE DROP CONSTRAINT，drop_all 解不出循環會炸。
    engine.dispose()


def _make_test_token(role: str, user_id: int, username: str) -> str:
    """產生測試用 JWT；與 test_portfolio_batch_a 同 pattern。"""
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


def test_create_measurement_success(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/measurements",
        json={
            "measured_on": date.today().isoformat(),
            "height_cm": "110.50",
            "weight_kg": "18.20",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["student_id"] == 1
    assert body["height_cm"] == "110.50"
    assert body["weight_kg"] == "18.20"


def test_create_measurement_requires_at_least_one_value(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/measurements",
        json={"measured_on": date.today().isoformat(), "note": "只填備註"},
    )
    assert resp.status_code == 422


def test_create_measurement_rejects_future_date(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/measurements",
        json={
            "measured_on": (date.today() + timedelta(days=1)).isoformat(),
            "height_cm": "100.00",
        },
    )
    assert resp.status_code == 422


def test_create_measurement_rejects_unreasonable_height(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/students/1/measurements",
        json={
            "measured_on": date.today().isoformat(),
            "height_cm": "250.00",  # 超過合理上限 200
        },
    )
    assert resp.status_code == 422


def test_list_measurements_orders_desc(app_client):
    client, _ = app_client
    today = date.today()
    for i in range(3):
        resp = client.post(
            "/api/students/1/measurements",
            json={
                "measured_on": (today - timedelta(days=i * 30)).isoformat(),
                "height_cm": f"{100 + i}.00",
            },
        )
        assert resp.status_code == 201
    resp = client.get("/api/students/1/measurements")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    # 最新日期排第一
    assert items[0]["measured_on"] > items[1]["measured_on"]


def test_update_measurement(app_client):
    client, _ = app_client
    created = client.post(
        "/api/students/1/measurements",
        json={
            "measured_on": date.today().isoformat(),
            "height_cm": "100.00",
        },
    ).json()
    resp = client.patch(
        f"/api/students/1/measurements/{created['id']}",
        json={"weight_kg": "20.00"},
    )
    assert resp.status_code == 200
    assert resp.json()["weight_kg"] == "20.00"
    assert resp.json()["height_cm"] == "100.00"  # 未動到


def test_delete_measurement_hard_deletes(app_client):
    client, session_factory = app_client
    created = client.post(
        "/api/students/1/measurements",
        json={
            "measured_on": date.today().isoformat(),
            "height_cm": "100.00",
        },
    ).json()
    resp = client.delete(f"/api/students/1/measurements/{created['id']}")
    assert resp.status_code == 204
    # 直接查 DB 確認硬刪
    with session_factory() as session:
        from models.database import StudentMeasurement

        assert session.query(StudentMeasurement).count() == 0


def test_chart_data_endpoint(app_client):
    client, _ = app_client
    today = date.today()
    for i in range(3):
        client.post(
            "/api/students/1/measurements",
            json={
                "measured_on": (today - timedelta(days=i * 90)).isoformat(),
                "height_cm": f"{100 + i * 3}.00",
                "weight_kg": f"{15 + i * 2}.00",
            },
        )
    resp = client.get("/api/students/1/measurements/chart-data")
    assert resp.status_code == 200
    data = resp.json()
    assert "height" in data and "weight" in data
    assert len(data["height"]) == 3
    # asc 排序方便 chart x 軸
    assert data["height"][0]["x"] < data["height"][-1]["x"]


def test_teacher_cannot_access_other_class_student(app_client, monkeypatch):
    """非自己班的學生 → 403"""
    client, session_factory = app_client
    with session_factory() as session:
        session.add_all(
            [
                Classroom(id=2, name="貓貓班", is_active=True),
                Student(
                    id=2,
                    student_id="S002",
                    name="李小華",
                    classroom_id=2,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                ),
                User(
                    id=10,
                    username="teacher_a",
                    password_hash="$2b$12$dummy",
                    role="teacher",
                    permissions=-1,
                    is_active=True,
                    token_version=0,
                ),
            ]
        )
        session.commit()
    teacher_token = _make_test_token(role="teacher", user_id=10, username="teacher_a")
    monkeypatch.setattr(
        "utils.portfolio_access.accessible_classroom_ids",
        lambda session, user: [1] if user["role"] == "teacher" else None,
    )
    client.headers.update({"Authorization": f"Bearer {teacher_token}"})
    resp = client.get("/api/students/2/measurements")
    assert resp.status_code == 403
