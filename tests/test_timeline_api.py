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
    # in-memory SQLite + StaticPool：dispose 即釋放連線、DB 隨之消失。
    # 不可用 drop_all：appraisal_cycles 等表存在 FK cycle，SQLite 不支援
    # ALTER TABLE DROP CONSTRAINT，drop_all 拓撲排序解不出循環會炸。
    engine.dispose()


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


def test_timeline_includes_assessments(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.database import StudentAssessment

        session.add(
            StudentAssessment(
                student_id=1,
                semester="2025下",
                assessment_type="期末",
                domain="認知",
                rating="優",
                content="測試評量內容",  # 欄位為 content 非 comment
                assessment_date=date.today(),
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    assert any(it["type"] == "assessment" for it in items)


def test_timeline_includes_incidents(app_client):
    from datetime import datetime

    client, session_factory = app_client
    with session_factory() as session:
        from models.database import StudentIncident

        session.add(
            StudentIncident(
                student_id=1,
                incident_type="意外受傷",  # 欄位為 incident_type 非 title
                severity="輕微",
                occurred_at=datetime.now(),  # 欄位為 occurred_at
                description="戶外活動時膝蓋擦傷",
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline?since=2020-01-01")
    items = resp.json()["items"]
    assert any(it["type"] == "incident" for it in items)


def test_timeline_includes_communications(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.student_log import ParentCommunicationLog

        session.add(
            ParentCommunicationLog(
                student_id=1,
                communication_date=date.today(),
                communication_type="電話",
                topic="家長詢問",  # 欄位為 topic 非 subject
                content="詢問下週活動",
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    assert any(it["type"] == "communication" for it in items)


def test_timeline_includes_contact_book(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.database import StudentContactBookEntry

        session.add(
            StudentContactBookEntry(
                student_id=1,
                classroom_id=1,
                log_date=date.today(),
                teacher_note="今天小華有把蘋果吃完",
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    assert any(it["type"] == "contact_book" for it in items)


def test_timeline_includes_attendance_only_when_abnormal(app_client):
    client, session_factory = app_client
    today = date.today()
    with session_factory() as session:
        from models.database import StudentAttendance

        session.add_all(
            [
                StudentAttendance(
                    student_id=1, date=today, status="出席"
                ),  # 正常 → 不應出現
                StudentAttendance(
                    student_id=1,
                    date=today - timedelta(days=1),
                    status="請假",
                ),  # 異常 → 出現
            ]
        )
        session.commit()
    resp = client.get("/api/students/1/timeline")
    items = resp.json()["items"]
    attendance_items = [it for it in items if it["type"] == "attendance"]
    assert len(attendance_items) == 1
    assert attendance_items[0]["extra"]["status"] == "請假"


def test_timeline_includes_activity(app_client):
    client, session_factory = app_client
    with session_factory() as session:
        from models.activity import ActivityRegistration

        session.add(
            ActivityRegistration(
                student_id=1,
                student_name="王小明",  # NOT NULL 欄位
            )
        )
        session.commit()
    resp = client.get("/api/students/1/timeline?since=2020-01-01")
    items = resp.json()["items"]
    assert any(it["type"] == "activity" for it in items)


def test_timeline_activity_includes_until_day(app_client):
    """round 5 P1：until=今天 應含今天的活動報名。

    bug：created_at(DateTime) <= until(Date) 等於 <= until 00:00:00，
    當天活動全被排除。fix：改半開區間 < until + 1 day。
    """
    from datetime import datetime

    client, session_factory = app_client
    with session_factory() as session:
        from models.activity import ActivityRegistration

        # 今天 14:30 報名（DateTime）
        now = datetime.utcnow().replace(hour=14, minute=30, second=0, microsecond=0)
        session.add(
            ActivityRegistration(
                student_id=1,
                student_name="王小明",
                created_at=now,
            )
        )
        session.commit()
    today_iso = date.today().isoformat()
    resp = client.get(f"/api/students/1/timeline?since=2020-01-01&until={today_iso}")
    items = resp.json()["items"]
    assert any(it["type"] == "activity" for it in items), items


def test_timeline_get_writes_read_audit(app_client):
    """F-V6-03：timeline 跨模組聚合 GET 必須留下 AuditLog action=READ 痕跡。"""
    import time

    from models.database import AuditLog

    client, session_factory = app_client
    resp = client.get("/api/students/1/timeline?types=milestone,observation")
    assert resp.status_code == 200, resp.text

    # write_explicit_audit 是 fire-and-forget；等背景寫入落地
    time.sleep(0.1)
    with session_factory() as session:
        rows = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "READ",
                AuditLog.entity_type == "student",
                AuditLog.entity_id == "1",
            )
            .all()
        )
    assert any(
        "portfolio timeline 跨模組聚合" in (r.summary or "") for r in rows
    ), f"未找到 portfolio timeline READ audit；rows={[(r.entity_id, r.summary) for r in rows]}"
