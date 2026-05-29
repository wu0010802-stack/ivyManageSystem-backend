"""Tests for admin announcement schedule fields (PR #1, T8)."""

import os
import sys
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.announcements import router as announcements_router
from models.database import Announcement, Base, Employee, User
from utils.auth import create_access_token, hash_password
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "ann-schedule.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(announcements_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_admin(session):
    """建立一個 admin user + employee，返回 (admin_user, employee)。"""
    emp = Employee(
        employee_id="SCHED-E001",
        name="排程管理員",
        base_salary=35000,
        is_active=True,
    )
    session.add(emp)
    session.flush()

    user = User(
        username="sched_admin",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=["*"],
        employee_id=emp.id,
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user, emp


def _admin_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permission_names": (
                user.permission_names if user.permission_names is not None else []
            ),
            "token_version": user.token_version or 0,
        }
    )


# ── Test 1: 無排程欄位 → status = active ──────────────────────────────────────


def test_list_returns_status_active_for_unscheduled(admin_client):
    """No publish_at + no expires_at → active."""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        a = Announcement(title="T", content="C", created_by=emp.id)
        session.add(a)
        session.commit()
        token = _admin_token(admin_user)
        ann_id = a.id

    res = client.get("/api/announcements", cookies={"access_token": token})
    assert res.status_code == 200
    items = res.json()["items"]
    me = next(i for i in items if i["id"] == ann_id)
    assert me["status"] == "active"
    assert me["publish_at"] is None
    assert me["expires_at"] is None


# ── Test 2: publish_at 在未來 → status = scheduled ───────────────────────────


def test_list_returns_scheduled_for_future_publish(admin_client):
    """publish_at 在未來 → status = scheduled。"""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        future = now_taipei_naive() + timedelta(hours=2)
        a = Announcement(title="T", content="C", created_by=emp.id, publish_at=future)
        session.add(a)
        session.commit()
        token = _admin_token(admin_user)
        ann_id = a.id

    res = client.get("/api/announcements", cookies={"access_token": token})
    assert res.status_code == 200
    items = res.json()["items"]
    me = next(i for i in items if i["id"] == ann_id)
    assert me["status"] == "scheduled"


# ── Test 3: expires_at 已過 → status = expired ───────────────────────────────


def test_list_returns_expired_for_past_expires(admin_client):
    """expires_at 已過期 → status = expired。"""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        past = now_taipei_naive() - timedelta(hours=1)
        a = Announcement(title="T", content="C", created_by=emp.id, expires_at=past)
        session.add(a)
        session.commit()
        token = _admin_token(admin_user)
        ann_id = a.id

    res = client.get("/api/announcements", cookies={"access_token": token})
    assert res.status_code == 200
    items = res.json()["items"]
    me = next(i for i in items if i["id"] == ann_id)
    assert me["status"] == "expired"


# ── Test 4: publish_at 在未來 → parent-recipients PUT 不立即推播 ─────────────


def test_create_with_publish_at_future_skips_immediate_push(admin_client, monkeypatch):
    """publish_at 在未來時 replace_parent_recipients 不應立即觸發 LINE enqueue。"""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        session.commit()
        token = _admin_token(admin_user)

    from api import announcements as ann_module

    calls = []
    monkeypatch.setattr(
        ann_module,
        "_fire_announcement_push",
        lambda *a, **kw: calls.append((a, kw)),
    )

    future = (now_taipei_naive() + timedelta(hours=2)).isoformat()
    create_res = client.post(
        "/api/announcements",
        json={
            "title": "排程公告",
            "content": "C",
            "priority": "normal",
            "publish_at": future,
        },
        cookies={"access_token": token},
    )
    assert create_res.status_code in (200, 201), create_res.text
    ann_id = create_res.json()["id"]

    put_res = client.put(
        f"/api/announcements/{ann_id}/parent-recipients",
        json={"recipients": [{"scope": "all"}]},
        cookies={"access_token": token},
    )
    assert put_res.status_code == 200, put_res.text
    assert calls == [], "publish_at 在未來時不應立即推播"


# ── Test 5: publish_at 已過 → parent-recipients PUT 立即推播 ─────────────────


def test_create_with_publish_at_past_immediate_push(admin_client, monkeypatch):
    """publish_at 已過（或已到）→ replace_parent_recipients 應立即觸發 LINE enqueue。"""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        session.commit()
        token = _admin_token(admin_user)

    from api import announcements as ann_module

    calls = []
    monkeypatch.setattr(
        ann_module,
        "_fire_announcement_push",
        lambda *a, **kw: calls.append((a, kw)),
    )

    # 使用直接建立 Announcement（past publish_at 不走 create endpoint 驗證）
    with session_factory() as session:
        past = now_taipei_naive() - timedelta(hours=1)
        a = Announcement(
            title="T",
            content="C",
            created_by=admin_user.employee_id,
            publish_at=past,
        )
        session.add(a)
        session.commit()
        ann_id = a.id

    put_res = client.put(
        f"/api/announcements/{ann_id}/parent-recipients",
        json={"recipients": [{"scope": "all"}]},
        cookies={"access_token": token},
    )
    assert put_res.status_code == 200, put_res.text
    assert len(calls) == 1, "publish_at 已到期應立即推播"


# ── Test 6: expires_at <= publish_at → 400 ───────────────────────────────────


def test_create_rejects_expires_before_publish(admin_client):
    """expires_at <= publish_at 時，create 應回 400。"""
    client, session_factory = admin_client
    with session_factory() as session:
        admin_user, emp = _seed_admin(session)
        session.commit()
        token = _admin_token(admin_user)

    base = now_taipei_naive()
    res = client.post(
        "/api/announcements",
        json={
            "title": "T",
            "content": "C",
            "priority": "normal",
            "publish_at": (base + timedelta(hours=2)).isoformat(),
            "expires_at": (base + timedelta(hours=1)).isoformat(),
        },
        cookies={"access_token": token},
    )
    assert res.status_code == 400, res.text
    detail = res.json().get("detail", "")
    assert (
        "expires" in detail.lower() or "到期" in detail or "發佈" in detail
    ), f"expected detail about schedule, got: {detail}"
