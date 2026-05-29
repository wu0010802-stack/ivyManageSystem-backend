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
                permission_names=["*"],
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
                "permission_names": ["*"],
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


# ── 全勤章（end-to-end，含官方工作日計算 wiring）─────────────────────────


def _april_2026_weekdays():
    from datetime import timedelta

    out = []
    d = date(2026, 4, 1)
    while d <= date(2026, 4, 30):
        if d.weekday() < 5:  # 無 seed 假日 → 平日即官方工作日
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed_attendance(TestingSession, student_id, dates, status="出席"):
    from models.database import StudentAttendance

    with TestingSession() as s:
        for d in dates:
            s.add(StudentAttendance(student_id=student_id, date=d, status=status))
        s.commit()


def test_auto_detect_perfect_attendance_all_workdays_present(app_client):
    client, TestingSession = app_client
    _seed_attendance(TestingSession, 1, _april_2026_weekdays())
    resp = client.post(
        "/api/students/1/milestones/auto-detect",
        json={"reference_date": "2026-05-01"},
    )
    assert resp.status_code == 200, resp.text
    list_resp = client.get(
        "/api/students/1/milestones?milestone_type=perfect_attendance_month"
    )
    items = list_resp.json()["items"]
    assert len(items) == 1, items
    assert items[0]["source_type"] == "auto_attendance"
    assert str(items[0]["achieved_on"]).startswith("2026-04")


def test_auto_detect_perfect_attendance_one_missing_workday_no_badge(app_client):
    client, TestingSession = app_client
    # 缺最後一個官方工作日 → 嚴格規則不發章
    _seed_attendance(TestingSession, 1, _april_2026_weekdays()[:-1])
    resp = client.post(
        "/api/students/1/milestones/auto-detect",
        json={"reference_date": "2026-05-01"},
    )
    assert resp.status_code == 200, resp.text
    list_resp = client.get(
        "/api/students/1/milestones?milestone_type=perfect_attendance_month"
    )
    assert list_resp.json()["items"] == [], list_resp.json()["items"]


def test_auto_detect_perfect_attendance_partial_first_month_no_badge(app_client):
    """月中才開始有記錄（前段工作日無記錄）→ 非「滿月」全勤，不發章。

    守住 caller 的工作日區間須以整月起算（非從第一筆記錄裁剪）。
    """
    client, TestingSession = app_client
    weekdays = [d for d in _april_2026_weekdays() if d >= date(2026, 4, 10)]
    _seed_attendance(TestingSession, 1, weekdays)
    resp = client.post(
        "/api/students/1/milestones/auto-detect",
        json={"reference_date": "2026-05-01"},
    )
    assert resp.status_code == 200, resp.text
    list_resp = client.get(
        "/api/students/1/milestones?milestone_type=perfect_attendance_month"
    )
    assert list_resp.json()["items"] == [], list_resp.json()["items"]
