"""Portal announcements time-predicate enforcement (PR #1, T9).

驗證 visible_filter 已套 publish_at/expires_at time predicate：
- 未來 publish_at 的公告不應出現在列表
- 已過 expires_at 的公告不應出現在列表
- 未來 publish_at 的公告 mark_read 應回 403
"""

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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portal.announcements import router as portal_announcements_router
from models.database import Announcement, Base, Employee, User
from utils.auth import create_access_token, hash_password
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def portal_ann_client(tmp_path):
    """隔離 sqlite 測試 app（僅含 auth + portal announcements）。"""
    db_path = tmp_path / "portal-ann-schedule.sqlite"
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

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(portal_announcements_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_teacher(session, employee_id: str = "TANN001", username: str = "ann_teacher"):
    """建立一個 teacher user + employee，回傳 (user, employee)。"""
    emp = Employee(
        employee_id=employee_id,
        name="公告教師",
        base_salary=35000,
        is_active=True,
    )
    session.add(emp)
    session.flush()

    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="teacher",
        permission_names=["ANNOUNCEMENTS_READ"],
        employee_id=emp.id,
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user, emp


def _teacher_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permission_names": user.permission_names or [],
            "token_version": user.token_version or 0,
        }
    )


def test_portal_hides_scheduled_announcement(portal_ann_client):
    """publish_at 在未來的公告不應出現在 portal list。"""
    client, session_factory = portal_ann_client
    with session_factory() as session:
        user, emp = _seed_teacher(session)
        future = now_taipei_naive() + timedelta(hours=2)
        a = Announcement(
            title="未來公告",
            content="C",
            created_by=emp.id,
            publish_at=future,
        )
        session.add(a)
        session.commit()
        token = _teacher_token(user)
        ann_id = a.id

    res = client.get(
        "/api/portal/announcements",
        cookies={"access_token": token},
    )
    assert res.status_code == 200
    ids = [i["id"] for i in res.json()["items"]]
    assert (
        ann_id not in ids
    ), f"排程公告（publish_at 未來）不應出現在 portal 列表，但 id={ann_id} 出現了"


def test_portal_hides_expired_announcement(portal_ann_client):
    """expires_at 已過的公告不應出現在 portal list。"""
    client, session_factory = portal_ann_client
    with session_factory() as session:
        user, emp = _seed_teacher(
            session, employee_id="TANN002", username="ann_teacher2"
        )
        past = now_taipei_naive() - timedelta(hours=1)
        a = Announcement(
            title="過期公告",
            content="C",
            created_by=emp.id,
            expires_at=past,
        )
        session.add(a)
        session.commit()
        token = _teacher_token(user)
        ann_id = a.id

    res = client.get(
        "/api/portal/announcements",
        cookies={"access_token": token},
    )
    assert res.status_code == 200
    ids = [i["id"] for i in res.json()["items"]]
    assert ann_id not in ids, f"已過期公告不應出現在 portal 列表，但 id={ann_id} 出現了"


def test_portal_mark_read_rejects_unpublished(portal_ann_client):
    """publish_at 在未來的公告嘗試 mark_read 應回 403。"""
    client, session_factory = portal_ann_client
    with session_factory() as session:
        user, emp = _seed_teacher(
            session, employee_id="TANN003", username="ann_teacher3"
        )
        future = now_taipei_naive() + timedelta(hours=2)
        a = Announcement(
            title="未來公告T",
            content="C",
            created_by=emp.id,
            publish_at=future,
        )
        session.add(a)
        session.commit()
        token = _teacher_token(user)
        ann_id = a.id

    res = client.post(
        f"/api/portal/announcements/{ann_id}/read",
        cookies={"access_token": token},
    )
    assert (
        res.status_code == 403
    ), f"publish_at 未來的公告 mark_read 應回 403，但得到 {res.status_code}: {res.text}"
