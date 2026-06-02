"""tests/consent/test_photo_publish_gate.py — photo_publish 廣播咽喉測試（Task 6A）。

行為（spec §3.1b）：
  flag on  + guardian 未同意 photo_publish → publish_entry 後不應在 dispatch.enqueue
              的 recipient_user_id 中出現該 guardian；已同意的仍在。
  flag off → 全部 guardian 皆收到（不過濾）。

設計決策：
  - 對 dispatch.enqueue 做 mock（同步，同 call stack，可靠斷言）
  - broadcast_parent 在 fire-and-forget asyncio 中，不確定性高，不做斷言
  - 一個學生兩個 guardian：guardian_consented（有 photo_publish log，consented=True）
    與 guardian_no_consent（無 log → False），flag on 時應只通知前者
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.portal.contact_book import init_contact_book_line_service
from models.consent import (
    CONSENT_SCOPE_PHOTO_PUBLISH,
    ParentConsentLog,
    PolicyVersion,
)
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    StudentContactBookEntry,
    User,
)
from utils.auth import create_access_token
from utils.cache_layer import reset_cache_for_testing
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive

# ── Cache 隔離 ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# ── App fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def photo_gate_app(tmp_path):
    """SQLite in-memory FastAPI + TestClient，含 exception_handlers。"""
    db_path = tmp_path / "photo_gate.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    line_service = MagicMock()
    line_service.should_push_to_parent.return_value = None
    init_contact_book_line_service(line_service)

    from api.portal import router as portal_router
    from api.parent_portal import parent_router
    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override
    from utils.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(portal_router)
    app.include_router(parent_router)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(sf)

    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()
    init_contact_book_line_service(None)


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _make_teacher(session, classroom_id: int):
    emp = Employee(
        employee_id="EP_PGT", name="照片老師", is_active=True, base_salary=30000
    )
    session.add(emp)
    session.flush()
    user = User(
        username="teacher_pg",
        password_hash="!hash",
        role="teacher",
        employee_id=emp.id,
        permission_names=[
            Permission.PORTFOLIO_READ.value,
            Permission.PORTFOLIO_WRITE.value,
        ],
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
    classroom.head_teacher_id = emp.id
    session.flush()
    return emp, user


def _make_parent_user(session, username: str) -> User:
    u = User(
        username=username,
        password_hash="!LINE",
        role="parent",
        permission_names=[],
        is_active=True,
        token_version=0,
    )
    session.add(u)
    session.flush()
    return u


def _seed_two_guardians(session):
    """建立：班級 + 學生 + 兩個 guardian（一個有 photo_publish 同意，一個沒有）。

    回傳：(emp, teacher_user, entry_id, consented_uid, no_consent_uid)
    """
    classroom = Classroom(name="照片班", is_active=True)
    session.add(classroom)
    session.flush()

    emp, teacher_user = _make_teacher(session, classroom.id)

    student = Student(
        student_id="SPG01",
        name="照片學生",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    # guardian_consented：有 photo_publish 同意
    parent_consented = _make_parent_user(session, "parent_consented")
    session.add(
        Guardian(
            student_id=student.id,
            user_id=parent_consented.id,
            name="同意家長",
            relation="父親",
            is_primary=True,
            can_pickup=True,
        )
    )

    # guardian_no_consent：無任何 consent log
    parent_no_consent = _make_parent_user(session, "parent_no_consent")
    session.add(
        Guardian(
            student_id=student.id,
            user_id=parent_no_consent.id,
            name="未同意家長",
            relation="母親",
            is_primary=False,
            can_pickup=False,
        )
    )
    session.flush()

    # 建 PolicyVersion + ParentConsentLog 給 parent_consented
    pv = PolicyVersion(
        version="2026.photo_test",
        effective_at=now_taipei_naive(),
        document_path="/policies/2026-photo-test.pdf",
    )
    session.add(pv)
    session.flush()

    session.add(
        ParentConsentLog(
            user_id=parent_consented.id,
            policy_version_id=pv.id,
            scope=CONSENT_SCOPE_PHOTO_PUBLISH,
            consented=True,
            consented_at=now_taipei_naive(),
        )
    )
    session.flush()

    # 建立 contact_book entry
    entry = StudentContactBookEntry(
        student_id=student.id,
        classroom_id=classroom.id,
        log_date=date(2026, 6, 2),
        teacher_note="今天很棒",
        created_by_employee_id=emp.id,
    )
    session.add(entry)
    session.flush()

    return emp, teacher_user, entry.id, parent_consented.id, parent_no_consent.id


def _teacher_token(teacher_user: User, emp: Employee) -> str:
    return create_access_token(
        {
            "user_id": teacher_user.id,
            "employee_id": emp.id,
            "role": "teacher",
            "name": teacher_user.username,
            "permission_names": teacher_user.permission_names,
            "token_version": teacher_user.token_version or 0,
        }
    )


# ── Task A 測試 ───────────────────────────────────────────────────────────────


def test_photo_publish_gate_flag_on_filters_non_consented(photo_gate_app, monkeypatch):
    """flag on + guardian 未同意 photo_publish → dispatch.enqueue 不呼叫該 uid。
    已同意的 guardian 仍然收到 enqueue。
    """
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = photo_gate_app
    with sf() as session:
        emp, teacher_user, entry_id, consented_uid, no_consent_uid = (
            _seed_two_guardians(session)
        )
        session.commit()
        token = _teacher_token(teacher_user, emp)

    with patch("services.notification.dispatch.enqueue") as mock_enqueue:
        resp = client.post(
            f"/api/portal/contact-book/{entry_id}/publish",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["published_at"] is not None

    # 收集所有 enqueue 呼叫的 recipient_user_id
    enqueued_uids = {
        c.kwargs["recipient_user_id"]
        for c in mock_enqueue.call_args_list
        if c.kwargs.get("event_type") == "parent.contact_book_published"
    }

    # flag on：未同意的 guardian 不應收到
    assert no_consent_uid not in enqueued_uids, (
        f"未同意 photo_publish 的 guardian（uid={no_consent_uid}）"
        f"不應出現在 dispatch.enqueue 呼叫中；實際呼叫對象：{enqueued_uids}"
    )
    # flag on：已同意的 guardian 仍應收到
    assert consented_uid in enqueued_uids, (
        f"已同意 photo_publish 的 guardian（uid={consented_uid}）"
        f"應出現在 dispatch.enqueue 呼叫中；實際呼叫對象：{enqueued_uids}"
    )

    # cleanup flag
    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    reset_for_tests()


def test_photo_publish_gate_flag_off_all_guardians_receive(photo_gate_app, monkeypatch):
    """flag off → 未同意的 guardian 也收到（不過濾）。"""
    from config import reset_for_tests

    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    reset_for_tests()

    client, sf = photo_gate_app
    with sf() as session:
        emp, teacher_user, entry_id, consented_uid, no_consent_uid = (
            _seed_two_guardians(session)
        )
        session.commit()
        token = _teacher_token(teacher_user, emp)

    with patch("services.notification.dispatch.enqueue") as mock_enqueue:
        resp = client.post(
            f"/api/portal/contact-book/{entry_id}/publish",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text

    enqueued_uids = {
        c.kwargs["recipient_user_id"]
        for c in mock_enqueue.call_args_list
        if c.kwargs.get("event_type") == "parent.contact_book_published"
    }

    # flag off：兩個 guardian 都應收到
    assert (
        consented_uid in enqueued_uids
    ), f"flag off 時已同意家長（uid={consented_uid}）應收到"
    assert (
        no_consent_uid in enqueued_uids
    ), f"flag off 時未同意家長（uid={no_consent_uid}）也應收到（flag off 不過濾）"
