"""終態 API 端點必須同步 lifecycle_status + terminal_entered_at（PDPA PII GC 前置）。

威脅（稽核 2026-06-03 P1#1/#2）：api/students.py 的 graduate_student / delete_student
端點直接改 status/is_active，繞過 utils.student_lifecycle.set_lifecycle_status，導致
lifecycle_status 仍停在 active、terminal_entered_at 永不寫入 → 家長 PII 365 天 GC
（services/pii_retention_scheduler）永遠不會被觸發（CLAUDE.md §9 不變式被打破）。

修法：兩個端點在改狀態時改走 set_lifecycle_status（mapped lifecycle 常數），
只補 lifecycle_status + terminal_entered_at，不重複寫 ChangeLog、不引入轉移驗證。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students_terminal_lifecycle_sync.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


WRITE_PERMS = ["STUDENTS_WRITE", "STUDENTS_READ"]


def _create_admin(session):
    u = User(
        username="admin_t",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=WRITE_PERMS,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client):
    return client.post(
        "/api/auth/login",
        json={"username": "admin_t", "password": "Passw0rd!"},
    )


def _seed_student(session, name="在讀生"):
    cls = Classroom(name="大班A", is_active=True)
    session.add(cls)
    session.flush()
    s = Student(
        student_id="S999",
        name=name,
        classroom_id=cls.id,
        is_active=True,
        parent_name="家長",
        parent_phone="0900-111-222",
        lifecycle_status="active",
    )
    session.add(s)
    session.flush()
    return s


@pytest.mark.parametrize(
    "status,expected_lifecycle",
    [
        ("已畢業", LIFECYCLE_GRADUATED),
        ("已轉出", LIFECYCLE_TRANSFERRED),
    ],
)
def test_graduate_endpoint_syncs_lifecycle_and_terminal_timestamp(
    students_client, status, expected_lifecycle
):
    client, sf = students_client
    with sf() as s:
        _create_admin(s)
        student = _seed_student(s)
        s.commit()
        sid = student.id

    assert _login(client).status_code == 200
    res = client.post(
        f"/api/students/{sid}/graduate",
        json={"graduation_date": "2026-07-31", "status": status},
    )
    assert res.status_code == 200, res.text

    with sf() as s:
        st = s.query(Student).filter(Student.id == sid).first()
        assert st.status == status
        assert st.is_active is False
        # 核心回歸斷言：終態必須同步 lifecycle_status + terminal_entered_at
        assert st.lifecycle_status == expected_lifecycle, (
            f"graduate({status}) 後 lifecycle_status 應為 {expected_lifecycle}，"
            f"實得 {st.lifecycle_status}（繞過 set_lifecycle_status）"
        )
        assert (
            st.terminal_entered_at is not None
        ), "graduate 後 terminal_entered_at 應被設定，否則 PII GC 永不觸發"


def test_delete_endpoint_syncs_lifecycle_and_terminal_timestamp(students_client):
    """軟刪除學生同樣須同步終態 lifecycle_status + terminal_entered_at。

    delete_student 設 status='已刪除' / is_active=False 但不動 lifecycle_status，
    與 graduate 同類繞過 → PII GC 永不觸發。軟刪語意對應終態 withdrawn。
    """
    client, sf = students_client
    with sf() as s:
        _create_admin(s)
        student = _seed_student(s)
        s.commit()
        sid = student.id

    assert _login(client).status_code == 200
    res = client.delete(f"/api/students/{sid}")
    assert res.status_code == 200, res.text

    with sf() as s:
        st = s.query(Student).filter(Student.id == sid).first()
        assert st.status == "已刪除"
        assert st.is_active is False
        assert (
            st.lifecycle_status == LIFECYCLE_WITHDRAWN
        ), f"軟刪後 lifecycle_status 應為 withdrawn，實得 {st.lifecycle_status}"
        assert (
            st.terminal_entered_at is not None
        ), "軟刪後 terminal_entered_at 應被設定，否則 PII GC 永不觸發"
