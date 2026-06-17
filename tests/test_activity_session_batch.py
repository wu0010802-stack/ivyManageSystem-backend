"""才藝點名場次批次建立 POST /attendance/sessions/batch。

依課程 meeting_weekday（或 body.weekday）在日期範圍內展開上課日批次建場次，取代逐堂
手動新增；同課同日已存在（uq）跳過計入 skipped_existing（冪等）。
"""

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
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import Base, ActivityCourse, ActivitySession, User
from utils.auth import hash_password


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "activity-session-batch.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed(session_factory, *, meeting_weekday=2, is_active=True):
    with session_factory() as s:
        s.add(
            User(
                username="act_admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
                is_active=True,
            )
        )
        course = ActivityCourse(
            name="圍棋",
            price=1000,
            capacity=30,
            is_active=is_active,
            school_year=114,
            semester=2,
            meeting_weekday=meeting_weekday,
        )
        s.add(course)
        s.flush()
        cid = course.id
        s.commit()
    return cid


def _login(client):
    r = client.post(
        "/api/auth/login", json={"username": "act_admin", "password": "TempPass123"}
    )
    assert r.status_code == 200, r.text


def _batch(client, **body):
    return client.post("/api/activity/attendance/sessions/batch", json=body)


# 2026-06 的週三：3, 10, 17, 24（共 4）；週一：1, 8, 15, 22, 29（共 5）


def test_expands_course_meeting_weekday(activity_client):
    client, sf = activity_client
    cid = _seed(sf, meeting_weekday=2)  # 週三
    _login(client)
    r = _batch(client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created_count"] == 4
    assert data["skipped_existing"] == 0
    assert data["weekday"] == 2
    assert data["created_dates"] == [
        "2026-06-03",
        "2026-06-10",
        "2026-06-17",
        "2026-06-24",
    ]


def test_weekday_override(activity_client):
    client, sf = activity_client
    cid = _seed(sf, meeting_weekday=2)
    _login(client)
    # 覆寫成週一 → 5 場
    r = _batch(
        client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30", weekday=0
    )
    assert r.status_code == 200, r.text
    assert r.json()["created_count"] == 5


def test_dedup_skips_existing_and_idempotent(activity_client):
    client, sf = activity_client
    cid = _seed(sf, meeting_weekday=2)
    # 預先建 6/10 一場
    with sf() as s:
        s.add(ActivitySession(course_id=cid, session_date=date(2026, 6, 10)))
        s.commit()
    _login(client)
    r1 = _batch(client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30")
    assert r1.status_code == 200
    assert r1.json()["created_count"] == 3
    assert r1.json()["skipped_existing"] == 1
    # 再跑一次 → 全部已存在（冪等）
    r2 = _batch(client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30")
    assert r2.json()["created_count"] == 0
    assert r2.json()["skipped_existing"] == 4
    # DB 實際只有 4 場
    with sf() as s:
        assert (
            s.query(ActivitySession).filter(ActivitySession.course_id == cid).count()
            == 4
        )


def test_missing_weekday_400(activity_client):
    client, sf = activity_client
    cid = _seed(sf, meeting_weekday=None)
    _login(client)
    r = _batch(client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30")
    assert r.status_code == 400
    assert "上課星期" in r.json()["detail"]


def test_end_before_start_400(activity_client):
    client, sf = activity_client
    cid = _seed(sf)
    _login(client)
    r = _batch(client, course_id=cid, start_date="2026-06-30", end_date="2026-06-01")
    assert r.status_code == 400


def test_range_too_large_400(activity_client):
    client, sf = activity_client
    cid = _seed(sf, meeting_weekday=2)
    _login(client)
    r = _batch(client, course_id=cid, start_date="2025-01-01", end_date="2027-12-31")
    assert r.status_code == 400
    assert "最多" in r.json()["detail"]


def test_inactive_course_404(activity_client):
    client, sf = activity_client
    cid = _seed(sf, is_active=False)
    _login(client)
    r = _batch(client, course_id=cid, start_date="2026-06-01", end_date="2026-06-30")
    assert r.status_code == 404
