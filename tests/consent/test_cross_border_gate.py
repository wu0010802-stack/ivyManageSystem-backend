"""tests/consent/test_cross_border_gate.py — cross_border 上傳咽喉整合測試。

覆蓋三個 case：
1. enforcement on + 主要 guardian 未同意 cross_border_transfer → POST 照片 403
2. enforcement on + 主要 guardian 已同意 cross_border_transfer → POST 照片 201
3. enforcement off（flag false）→ POST 照片 201（不擋）
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.parent_portal import parent_router
from api.portal import router as portal_router
from api.portal.contact_book import init_contact_book_line_service
from models.auth import User
from models.consent import (
    CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
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
)
from utils.auth import create_access_token
from utils.cache_layer import reset_cache_for_testing
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive


def _make_real_png() -> bytes:
    """用 Pillow 產生 1×1 px 真實 PNG，可過 validate_file_signature + strip_image_metadata。"""
    import io

    from PIL import Image

    img = Image.new("RGB", (1, 1), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_MIN_PNG = _make_real_png()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    """每個 test 前後清 consent cache，避免跨 test 污染。"""
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


@pytest.fixture
def gate_client(tmp_path):
    """建 FastAPI + SQLite + TestClient，含 exception_handlers（確保 403 正確格式）。"""
    db_path = tmp_path / "cross_border_gate.sqlite"
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

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(portal_router)
    app.include_router(parent_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(sf)

    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()
    init_contact_book_line_service(None)


# ── Seed helpers ─────────────────────────────────────────────────────────────


def _make_policy(session) -> PolicyVersion:
    pv = PolicyVersion(
        version="2026.crossborder_test",
        effective_at=now_taipei_naive(),
        document_path="/policies/2026-crossborder-test.pdf",
    )
    session.add(pv)
    session.flush()
    return pv


def _seed(session):
    """建立：教師（班主任）+ 班級 + 學生 + 家長 user + guardian（primary）。"""
    emp = Employee(employee_id="T_CB", name="陳老師", is_active=True, base_salary=30000)
    session.add(emp)
    session.flush()

    classroom = Classroom(name="跨境班", is_active=True, head_teacher_id=emp.id)
    session.add(classroom)
    session.flush()

    teacher_user = User(
        username="teacher_cb",
        password_hash="!",
        role="teacher",
        employee_id=emp.id,
        permission_names=[
            Permission.PORTFOLIO_READ.value,
            Permission.PORTFOLIO_WRITE.value,
        ],
        is_active=True,
        token_version=0,
    )
    session.add(teacher_user)
    session.flush()

    student = Student(
        student_id="SCB01",
        name="跨境小孩",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    parent_user = User(
        username="parent_cb",
        password_hash="!",
        role="parent",
        permission_names=[],
        is_active=True,
        token_version=0,
    )
    session.add(parent_user)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=parent_user.id,
        name="跨境家長",
        relation="母親",
        is_primary=True,
        can_pickup=True,
    )
    session.add(guardian)
    session.flush()

    return emp, teacher_user, classroom, student, parent_user


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


def _make_contact_book_entry(
    session, student_id: int, classroom_id: int
) -> StudentContactBookEntry:
    from datetime import date

    entry = StudentContactBookEntry(
        student_id=student_id,
        classroom_id=classroom_id,
        log_date=date(2026, 6, 2),
    )
    session.add(entry)
    session.flush()
    return entry


def _fake_storage():
    """回傳 mock storage，put_attachment 回 StoredAttachment-like 物件。"""
    stored = SimpleNamespace(
        storage_key="test/2026/06/fake.png",
        display_key="test/2026/06/fake_display.jpg",
        thumb_key="test/2026/06/fake_thumb.jpg",
        mime_type="image/png",
    )
    mock_storage = MagicMock()
    mock_storage.put_attachment.return_value = stored
    return mock_storage


def _do_upload(client: TestClient, entry_id: int, token: str):
    """執行照片上傳，回傳 response。"""
    return client.post(
        f"/api/portal/contact-book/{entry_id}/photos",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test.png", _MIN_PNG, "image/png")},
    )


# ── Case 1：enforcement on + 未同意 → 403 ────────────────────────────────────


def test_upload_photo_blocked_when_no_consent(gate_client, monkeypatch):
    """consent flag on + 主要 guardian 無 cross_border_transfer 同意 → 403 CONSENT_REQUIRED。"""
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        emp, teacher_user, classroom, student, parent_user = _seed(session)
        entry = _make_contact_book_entry(session, student.id, classroom.id)
        session.commit()
        entry_id = entry.id
        token = _teacher_token(teacher_user, emp)

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = _do_upload(client, entry_id, token)

    assert resp.status_code == 403, f"預期 403，實際 {resp.status_code}: {resp.text}"
    body = resp.json()
    # detail 或 code 應含 consent 相關字眼
    detail_str = str(body).lower()
    assert (
        "consent" in detail_str or "同意" in detail_str
    ), f"回應應含 consent 相關字眼：{body}"


# ── Case 2：enforcement on + 已同意 → 201 ────────────────────────────────────


def test_upload_photo_allowed_when_consented(gate_client, monkeypatch):
    """consent flag on + 主要 guardian 已同意 cross_border_transfer → 201。"""
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        emp, teacher_user, classroom, student, parent_user = _seed(session)
        pv = _make_policy(session)
        # 寫入主要 guardian 已同意的 log
        log = ParentConsentLog(
            user_id=parent_user.id,
            policy_version_id=pv.id,
            scope=CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
            consented=True,
            consented_at=now_taipei_naive(),
        )
        session.add(log)
        entry = _make_contact_book_entry(session, student.id, classroom.id)
        session.commit()
        entry_id = entry.id
        token = _teacher_token(teacher_user, emp)

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = _do_upload(client, entry_id, token)

    assert resp.status_code == 201, f"預期 201，實際 {resp.status_code}: {resp.text}"


# ── Case 3：enforcement off → 201（不擋）──────────────────────────────────────


def test_upload_photo_allowed_when_enforcement_disabled(gate_client, monkeypatch):
    """consent flag off → 不擋，直接 201（無需 consent log）。"""
    from config import reset_for_tests

    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        emp, teacher_user, classroom, student, _parent_user = _seed(session)
        entry = _make_contact_book_entry(session, student.id, classroom.id)
        session.commit()
        entry_id = entry.id
        token = _teacher_token(teacher_user, emp)

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = _do_upload(client, entry_id, token)

    assert resp.status_code == 201, f"預期 201，實際 {resp.status_code}: {resp.text}"


# ── Case 4：leaves upload — enforcement on + 未同意 → 403 ─────────────────────


def test_leave_attachment_blocked_when_no_consent(gate_client, monkeypatch):
    """家長上傳請假附件：consent flag on + 主要 guardian 未同意 → 403。"""
    from datetime import date, timedelta

    from config import reset_for_tests
    from models.database import StudentLeaveRequest

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        _emp, _teacher_user, _classroom, student, parent_user = _seed(session)
        # 建立 approved + 未來日期的假單（upload guard 條件）
        future_date = date.today() + timedelta(days=3)
        lr = StudentLeaveRequest(
            student_id=student.id,
            applicant_user_id=parent_user.id,
            leave_type="病假",
            start_date=future_date,
            end_date=future_date,
            status="approved",
        )
        session.add(lr)
        session.commit()
        leave_id = lr.id
        parent_token = create_access_token(
            {
                "user_id": parent_user.id,
                "employee_id": None,
                "role": "parent",
                "name": parent_user.username,
                "permission_names": [],
                "token_version": parent_user.token_version or 0,
            }
        )

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = client.post(
            f"/api/parent/student-leaves/{leave_id}/attachments",
            headers={"Authorization": f"Bearer {parent_token}"},
            files={"file": ("doc.png", _MIN_PNG, "image/png")},
        )

    assert resp.status_code == 403, f"預期 403，實際 {resp.status_code}: {resp.text}"
    detail_str = str(resp.json()).lower()
    assert (
        "consent" in detail_str or "同意" in detail_str
    ), f"回應應含 consent 字眼：{resp.json()}"


# ── Case 5：medications upload — enforcement on + 未同意 → 403 ────────────────


def test_medication_photo_blocked_when_no_consent(gate_client, monkeypatch):
    """家長上傳用藥照：consent flag on + 主要 guardian 未同意 → 403。"""
    from datetime import date

    from config import reset_for_tests
    from models.portfolio import MEDICATION_SOURCE_PARENT, StudentMedicationOrder

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        _emp, _teacher_user, _classroom, student, parent_user = _seed(session)
        order = StudentMedicationOrder(
            student_id=student.id,
            order_date=date.today(),
            medication_name="感冒藥",
            dose="1 顆",
            time_slots=["12:00"],
            source=MEDICATION_SOURCE_PARENT,
            created_by=parent_user.id,
        )
        session.add(order)
        session.commit()
        order_id = order.id
        parent_token = create_access_token(
            {
                "user_id": parent_user.id,
                "employee_id": None,
                "role": "parent",
                "name": parent_user.username,
                "permission_names": [],
                "token_version": parent_user.token_version or 0,
            }
        )

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = client.post(
            f"/api/parent/medication-orders/{order_id}/photos",
            headers={"Authorization": f"Bearer {parent_token}"},
            files={"file": ("doc.png", _MIN_PNG, "image/png")},
        )

    assert resp.status_code == 403, f"預期 403，實際 {resp.status_code}: {resp.text}"
    detail_str = str(resp.json()).lower()
    assert (
        "consent" in detail_str or "同意" in detail_str
    ), f"回應應含 consent 字眼：{resp.json()}"


# ── Case 6：messages attach — enforcement on + 未同意 → 403 ──────────────────


def test_message_attach_blocked_when_no_consent(gate_client, monkeypatch):
    """家長訊息附件上傳：consent flag on + 主要 guardian 未同意 → 403。"""
    from config import reset_for_tests
    from models.database import ParentMessage, ParentMessageThread

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        _emp, teacher_user, _classroom, student, parent_user = _seed(session)
        thread = ParentMessageThread(
            parent_user_id=parent_user.id,
            teacher_user_id=teacher_user.id,
            student_id=student.id,
        )
        session.add(thread)
        session.flush()
        msg = ParentMessage(
            thread_id=thread.id,
            sender_user_id=parent_user.id,
            sender_role="parent",
            body="測試訊息",
        )
        session.add(msg)
        session.commit()
        thread_id = thread.id
        msg_id = msg.id
        parent_token = create_access_token(
            {
                "user_id": parent_user.id,
                "employee_id": None,
                "role": "parent",
                "name": parent_user.username,
                "permission_names": [],
                "token_version": parent_user.token_version or 0,
            }
        )

    with patch(
        "utils.portfolio_storage.get_portfolio_storage", return_value=_fake_storage()
    ):
        resp = client.post(
            f"/api/parent/messages/threads/{thread_id}/messages/{msg_id}/attach",
            headers={"Authorization": f"Bearer {parent_token}"},
            files={"file": ("doc.png", _MIN_PNG, "image/png")},
        )

    assert resp.status_code == 403, f"預期 403，實際 {resp.status_code}: {resp.text}"
    detail_str = str(resp.json()).lower()
    assert (
        "consent" in detail_str or "同意" in detail_str
    ), f"回應應含 consent 字眼：{resp.json()}"
