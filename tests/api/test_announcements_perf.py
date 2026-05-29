"""Admin announcements list perf — SQL COUNT + batch preview."""

import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.announcements import router as announcements_router
from models.database import (
    Announcement,
    AnnouncementRead,
    AnnouncementRecipient,
    Base,
    Employee,
    User,
)
from utils.auth import create_access_token, hash_password


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "ann-perf.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def admin_emp(db_session):
    emp = Employee(
        employee_id="PERF-ADMIN",
        name="perf_admin",
        is_active=True,
        base_salary=0,
    )
    db_session.add(emp)
    db_session.flush()
    return emp


@pytest.fixture
def admin_client(db_engine, admin_emp, db_session):
    factory = sessionmaker(bind=db_engine)

    user = User(
        username="perf_admin_user",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=["*"],
        employee_id=admin_emp.id,
        is_active=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()

    token = create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permission_names": user.permission_names or [],
            "token_version": user.token_version or 0,
        }
    )

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = factory

    app = FastAPI()
    app.include_router(announcements_router)

    with TestClient(app) as client:
        client.cookies.set("access_token", token)
        yield client

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory


def test_list_returns_read_count_recipient_count(admin_client, db_session, admin_emp):
    """list endpoint 回 read_count + recipient_count，不回完整 readers/recipient_ids。"""
    other = Employee(employee_id="E_OTHER", name="other", is_active=True, base_salary=0)
    db_session.add(other)
    db_session.flush()

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=other.id))
    db_session.add(AnnouncementRead(announcement_id=a.id, employee_id=other.id))
    db_session.commit()

    res = admin_client.get("/api/announcements")
    assert res.status_code == 200
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert item["read_count"] == 1
    assert item["recipient_count"] == 1
    assert "readers" not in item
    assert "recipient_ids" not in item
    assert len(item["read_preview"]) == 1
    assert item["read_preview"][0]["employee_id"] == other.id


def test_list_read_preview_top3_by_read_at_desc(admin_client, db_session, admin_emp):
    """read_preview 最多 3 筆，依 read_at DESC 排序；has_more_readers 正確。"""
    emps = []
    for i in range(5):
        e = Employee(employee_id=f"E_R{i}", name=f"e{i}", is_active=True, base_salary=0)
        db_session.add(e)
        db_session.flush()
        emps.append(e)

    a = Announcement(title="T2", content="C2", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()

    base = datetime(2026, 5, 29, 8, 0, 0)
    for i, e in enumerate(emps):
        db_session.add(
            AnnouncementRead(
                announcement_id=a.id,
                employee_id=e.id,
                read_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    res = admin_client.get("/api/announcements")
    assert res.status_code == 200
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert item["read_count"] == 5
    assert len(item["read_preview"]) == 3
    preview_ids = [p["employee_id"] for p in item["read_preview"]]
    assert preview_ids == [emps[4].id, emps[3].id, emps[2].id]
    assert item["has_more_readers"] is True


def test_recipients_endpoint_returns_employee_ids(admin_client, db_session, admin_emp):
    from models.database import Announcement, AnnouncementRecipient, Employee

    e1 = Employee(
        employee_id="E_E1", name="e1", is_active=True, base_salary=0
    )
    e2 = Employee(
        employee_id="E_E2", name="e2", is_active=True, base_salary=0
    )
    db_session.add_all([e1, e2])
    db_session.flush()

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=e1.id))
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=e2.id))
    db_session.commit()

    res = admin_client.get(f"/api/announcements/{a.id}/recipients")
    assert res.status_code == 200
    assert set(res.json()["employee_ids"]) == {e1.id, e2.id}


def test_recipients_endpoint_returns_404_for_unknown(admin_client):
    res = admin_client.get("/api/announcements/999999/recipients")
    assert res.status_code == 404


def test_readers_endpoint_returns_paged_list_desc(
    admin_client, db_session, admin_emp
):
    from datetime import datetime, timedelta
    from models.database import Announcement, AnnouncementRead, Employee

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    base = datetime(2026, 5, 29, 8, 0, 0)
    emps = []
    for i in range(7):
        e = Employee(
            employee_id=f"E_RD{i}", name=f"r{i}", is_active=True, base_salary=0
        )
        db_session.add(e)
        db_session.flush()
        emps.append(e)
        db_session.add(
            AnnouncementRead(
                announcement_id=a.id,
                employee_id=e.id,
                read_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    res = admin_client.get(f"/api/announcements/{a.id}/readers?page=1&page_size=3")
    body = res.json()
    assert body["total"] == 7
    assert body["page"] == 1
    assert body["page_size"] == 3
    assert len(body["items"]) == 3
    assert [it["employee_id"] for it in body["items"]] == [
        emps[6].id, emps[5].id, emps[4].id,
    ]

    res2 = admin_client.get(f"/api/announcements/{a.id}/readers?page=3&page_size=3")
    body2 = res2.json()
    assert len(body2["items"]) == 1


def test_readers_endpoint_404_for_unknown(admin_client):
    res = admin_client.get("/api/announcements/999999/readers")
    assert res.status_code == 404


def test_list_query_count_baseline(admin_client, db_engine, db_session, admin_emp):
    """100 公告 x 50 已讀 fixture 下，list endpoint 應只發 <= 4 SELECT。

    防 N+1 regression：correlated COUNT subquery + batch preview query 設計確保
    query 數固定不隨資料量退化。
    """
    from sqlalchemy import event

    readers = []
    for i in range(50):
        e = Employee(
            employee_id=f"E_BR{i}",
            name=f"r{i}",
            is_active=True,
            base_salary=0,
        )
        db_session.add(e)
        readers.append(e)
    db_session.flush()

    announcements = []
    for i in range(100):
        a = Announcement(title=f"T{i}", content="C", created_by=admin_emp.id)
        db_session.add(a)
        announcements.append(a)
    db_session.flush()

    for a in announcements:
        for r in readers:
            db_session.add(
                AnnouncementRead(announcement_id=a.id, employee_id=r.id)
            )
    db_session.commit()

    queries: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db_engine, "before_cursor_execute", _capture)
    try:
        res = admin_client.get("/api/announcements?page=1&page_size=50")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) == 50
    finally:
        event.remove(db_engine, "before_cursor_execute", _capture)

    selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
    # 5 fixed SELECTs: jwt_blocklist + users (auth) + COUNT + main + batch preview
    assert len(selects) <= 5, (
        f"too many SELECT queries: {len(selects)}\n"
        + "\n---\n".join(selects)
    )
