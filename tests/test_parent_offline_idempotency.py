"""家長端離線 queue 對應 2 endpoint 的 idempotency 整合測試。

兩個 endpoint 都應該：
- 接受 optional client_request_id (String(64))
- 同 client_request_id 重複 POST → 200/201 + 回原紀錄 + DB row count 不增加
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from models.contact_book import StudentContactBookReply
from models.database import (
    Base,
    Classroom,
    Guardian,
    Student,
    StudentContactBookEntry,
    StudentLeaveRequest,
    User,
)
from utils.auth import create_access_token


@pytest.fixture
def idempotency_client(tmp_path):
    """共用 fixture：SQLite in-memory DB + parent_router TestClient。"""
    db_path = tmp_path / "idempotency.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


# ── 工具：建立 fixture 資料 ─────────────────────────────────────────────────


def _setup_parent_with_entry(session):
    """建立家長 + 學生 + Guardian + 聯絡簿 entry（已發布）。
    回傳 (parent_token, entry_id)。
    """
    user = User(
        username="parent_idem",
        password_hash="!LINE",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id="U_idem",
        token_version=0,
    )
    session.add(user)
    session.flush()

    classroom = Classroom(name="測試班", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="ST_idem",
        name="測試生",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="家長",
            relation="父親",
            is_primary=True,
            can_pickup=True,
        )
    )
    session.flush()

    from datetime import datetime

    entry = StudentContactBookEntry(
        student_id=student.id,
        classroom_id=classroom.id,
        log_date=date.today(),
        mood="happy",
        published_at=datetime.now(),
    )
    session.add(entry)
    session.flush()

    token = create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": user.token_version or 0,
        }
    )
    return token, entry.id


def _setup_parent_with_student(session):
    """建立家長 + 學生 + Guardian。回傳 (parent_token, student_id)。"""
    user = User(
        username="parent_leave_idem",
        password_hash="!LINE",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id="U_leave_idem",
        token_version=0,
    )
    session.add(user)
    session.flush()

    classroom = Classroom(name="請假班", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="ST_leave_idem",
        name="請假生",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="家長",
            relation="父親",
            is_primary=True,
            can_pickup=True,
        )
    )
    session.flush()

    token = create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": user.token_version or 0,
        }
    )
    return token, student.id


# ─────────────────────────────────────────────────────────────────────────
# 測試：聯絡簿 reply idempotency
# ─────────────────────────────────────────────────────────────────────────


def test_contact_book_reply_repeat_client_request_id_returns_original(
    idempotency_client,
):
    """重複 POST 同 client_request_id → 回原 reply，DB row count 不增加。"""
    client, sf = idempotency_client
    with sf() as session:
        token, entry_id = _setup_parent_with_entry(session)
        session.commit()

    cu = str(uuid.uuid4())
    body = {"body": "重複回覆測試", "client_request_id": cu}

    r1 = client.post(
        f"/api/parent/contact-book/{entry_id}/reply",
        json=body,
        cookies={"access_token": token},
    )
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["id"]

    # 第二次 POST 同 client_request_id
    r2 = client.post(
        f"/api/parent/contact-book/{entry_id}/reply",
        json=body,
        cookies={"access_token": token},
    )
    assert r2.status_code in (200, 201), r2.text
    assert r2.json()["id"] == first_id, "重複 POST 應回原 reply"

    with sf() as session:
        count = session.scalar(
            select(func.count(StudentContactBookReply.id)).where(
                StudentContactBookReply.client_request_id == cu
            )
        )
    assert count == 1, f"DB 中應只有 1 筆，實際有 {count} 筆"


def test_contact_book_reply_no_client_request_id_still_works(idempotency_client):
    """既有 caller 沒 client_request_id 應正常運作（向後相容）。"""
    client, sf = idempotency_client
    with sf() as session:
        token, entry_id = _setup_parent_with_entry(session)
        session.commit()

    r = client.post(
        f"/api/parent/contact-book/{entry_id}/reply",
        json={"body": "沒有 UUID 的回覆"},
        cookies={"access_token": token},
    )
    assert r.status_code == 201, r.text


# ─────────────────────────────────────────────────────────────────────────
# 測試：學生請假 idempotency
# ─────────────────────────────────────────────────────────────────────────


def test_parent_leave_repeat_client_request_id_returns_original(idempotency_client):
    """重複 POST 同 client_request_id → 回原請假，DB row count 不增加。"""
    client, sf = idempotency_client
    with sf() as session:
        token, student_id = _setup_parent_with_student(session)
        session.commit()

    cu = str(uuid.uuid4())
    # 使用 7 天後避免日期限制邊界問題
    future_date = (date.today() + timedelta(days=7)).isoformat()
    body = {
        "student_id": student_id,
        "leave_type": "病假",
        "start_date": future_date,
        "end_date": future_date,
        "reason": "idempotent 測試",
        "client_request_id": cu,
    }

    r1 = client.post(
        "/api/parent/student-leaves", json=body, cookies={"access_token": token}
    )
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["id"]

    # 第二次 POST 同 client_request_id
    r2 = client.post(
        "/api/parent/student-leaves", json=body, cookies={"access_token": token}
    )
    assert r2.status_code in (200, 201), r2.text
    assert r2.json()["id"] == first_id, "重複 POST 應回原請假"

    with sf() as session:
        count = session.scalar(
            select(func.count(StudentLeaveRequest.id)).where(
                StudentLeaveRequest.client_request_id == cu
            )
        )
    assert count == 1, f"DB 中應只有 1 筆，實際有 {count} 筆"


def test_parent_leave_no_client_request_id_still_works(idempotency_client):
    """既有 caller 沒 client_request_id 應正常運作（向後相容）。"""
    client, sf = idempotency_client
    with sf() as session:
        token, student_id = _setup_parent_with_student(session)
        session.commit()

    future_date = (date.today() + timedelta(days=7)).isoformat()
    r = client.post(
        "/api/parent/student-leaves",
        json={
            "student_id": student_id,
            "leave_type": "事假",
            "start_date": future_date,
            "end_date": future_date,
            "reason": "沒有 UUID",
        },
        cookies={"access_token": token},
    )
    assert r.status_code == 201, r.text
