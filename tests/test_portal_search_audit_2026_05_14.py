"""Bug Sweep Round 4 (2026-05-14) — B2/B3/B4 回歸測試。

涵蓋三條：
- B2 [P1] /api/portal/search 必須寫 READ audit（跨班 PII 不可無痕）
- B3 [P1] /api/portal/students/measurements-latest 必須寫 READ audit（健康資料）
- B4 [P2] /api/portal/search announcements section 必須過濾 AnnouncementRecipient
       （否則教師可探勘僅發給 HR/supervisor 的定向公告標題）
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    AuditLog,
    Base,
    Classroom,
    Employee,
    Student,
    StudentMeasurement,
    User,
)
from models.event import Announcement, AnnouncementRecipient
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "portal-audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    app = FastAPI()
    app.include_router(portal_router)
    client = TestClient(app)
    yield client, sf
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_teacher(sess, classroom):
    emp = Employee(employee_id="T001", name="王老師", is_active=True)
    sess.add(emp)
    sess.flush()
    classroom.head_teacher_id = emp.id
    sess.flush()
    user = User(
        username="wang",
        password_hash="x",
        role="teacher",
        employee_id=emp.id,
        permissions=int(Permission.PARENT_MESSAGES_WRITE)
        | int(Permission.PORTFOLIO_READ),
        is_active=True,
        token_version=0,
    )
    sess.add(user)
    sess.flush()
    token = create_access_token(
        {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "employee_id": emp.id,
            "permissions": user.permissions,
            "token_version": 0,
        }
    )
    return emp, user, token


def _fetch_audit(sf, entity_type: str):
    # write_explicit_audit 走 fire-and-forget background queue（_schedule_audit_write）
    # 給 background worker 一點時間完成寫入
    for _ in range(30):
        time.sleep(0.05)
        sess = sf()
        try:
            rows = (
                sess.query(AuditLog)
                .filter(
                    AuditLog.action == "READ",
                    AuditLog.entity_type == entity_type,
                )
                .all()
            )
            if rows:
                return rows
        finally:
            sess.close()
    return []


# ── B2 ────────────────────────────────────────────────────────────────


def test_portal_search_writes_explicit_audit(client_sf):
    """teacher 呼叫 /api/portal/search → 必須留 READ portal_search audit。"""
    client, sf = client_sf
    sess = sf()
    cr = Classroom(name="A班", is_active=True)
    sess.add(cr)
    sess.flush()
    s = Student(
        student_id="A1",
        name="小明",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(s)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()
    sess.close()

    resp = client.get(
        "/api/portal/search?q=小明",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    rows = _fetch_audit(sf, "portal_search")
    assert rows, "/api/portal/search 應寫 entity_type=portal_search 的 READ audit"
    assert "小明" in rows[0].summary or "q=" in rows[0].summary


# ── B3 ────────────────────────────────────────────────────────────────


def test_measurements_latest_writes_explicit_audit(client_sf):
    """teacher 呼叫 /api/portal/students/measurements-latest → 必須留 READ audit。"""
    client, sf = client_sf
    sess = sf()
    cr = Classroom(name="A班", is_active=True)
    sess.add(cr)
    sess.flush()
    s = Student(
        student_id="A1",
        name="小華",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(s)
    sess.flush()
    m = StudentMeasurement(
        student_id=s.id,
        measured_on=date(2026, 4, 1),
        height_cm="100.0",
        weight_kg="18.5",
    )
    sess.add(m)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()
    sess.close()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1

    rows = _fetch_audit(sf, "student_measurement")
    assert (
        rows
    ), "/api/portal/students/measurements-latest 應寫 entity_type=student_measurement 的 READ audit"
    assert "n=1" in rows[0].summary


# ── B4 ────────────────────────────────────────────────────────────────


def test_search_excludes_targeted_announcements_not_for_me(client_sf):
    """teacher 不該透過 search 看到僅發給其他人的定向公告標題。"""
    client, sf = client_sf
    sess = sf()
    cr = Classroom(name="A班", is_active=True)
    sess.add(cr)
    sess.flush()
    emp, user, token = _seed_teacher(sess, cr)

    # 一條全員公告（無 recipients）
    ann_public = Announcement(
        title="全員公告：薪資政策說明",
        content="全員可見",
        created_by=emp.id,
        created_at=datetime(2026, 5, 1, 10, 0),
    )
    sess.add(ann_public)
    sess.flush()

    # 一條僅發給 HR_employee 的定向公告
    hr_emp = Employee(employee_id="HR1", name="HR小姐", is_active=True)
    sess.add(hr_emp)
    sess.flush()
    ann_targeted = Announcement(
        title="HR 內部：薪資調整名單",
        content="不該被 teacher 看到",
        created_by=hr_emp.id,
        created_at=datetime(2026, 5, 1, 11, 0),
    )
    sess.add(ann_targeted)
    sess.flush()
    sess.add(
        AnnouncementRecipient(announcement_id=ann_targeted.id, employee_id=hr_emp.id)
    )
    sess.commit()
    sess.close()

    resp = client.get(
        "/api/portal/search?q=薪資",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    titles = {a["title"] for a in resp.json()["announcements"]}
    assert "全員公告：薪資政策說明" in titles
    assert (
        "HR 內部：薪資調整名單" not in titles
    ), "定向公告必須被 AnnouncementRecipient 過濾擋下"
