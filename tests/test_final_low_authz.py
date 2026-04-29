"""IDOR audit Phase 2 L2：最後 2 個 Low finding（F-035 + F-044）。

涵蓋：
- F-035 audit/audit-logs/export：補 write_explicit_audit，匯出全系統審計
  軌跡的事件本身留下軌跡（對齊 F-033 的 export endpoints 模式）
- F-044 dismissal_calls/{call_id}/cancel：補 originator 守衛，僅原建立者
  或管理角色（admin/hr/supervisor）可取消；其他持 STUDENTS_WRITE 的角色
  不再可任意取消他人接送通知
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.audit as audit_module
import api.dismissal_calls as dismissal_calls_module
from api.audit import router as audit_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.dismissal_calls import router as dismissal_calls_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    AuditLog,
    Base,
    Classroom,
    Employee,
    Student,
    User,
)
from models.dismissal import StudentDismissalCall
from utils.auth import hash_password
from utils.permissions import Permission

# ─────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────


def _create_user(
    session,
    *,
    username: str,
    role: str,
    permissions: int,
    employee_id: int | None = None,
    password: str = "Pass1234",
) -> User:
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=int(permissions),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _create_employee(session, code: str, name: str) -> Employee:
    emp = Employee(
        employee_id=code,
        name=name,
        base_salary=32000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client: TestClient, username: str, password: str = "Pass1234") -> None:
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────
# F-035：audit-logs export 自身需呼叫 write_explicit_audit
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f035_client(tmp_path):
    db_path = tmp_path / "f035.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(audit_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f035(session) -> None:
    """admin 帳號 + 1 筆既有 AuditLog（讓匯出至少有 1 筆 row 可寫）。"""
    _create_user(
        session,
        username="adm_audit",
        role="admin",
        permissions=-1,
    )
    session.add(
        AuditLog(
            user_id=999,
            username="someone",
            action="UPDATE",
            entity_type="employee",
            entity_id="1",
            summary="test entry",
            ip_address="127.0.0.1",
            created_at=datetime.now(),
        )
    )
    session.commit()


class TestF035_AuditLogExport:
    """匯出 audit-logs 自身呼叫 write_explicit_audit。"""

    def test_export_writes_explicit_audit_record(self, f035_client, monkeypatch):
        client, sf = f035_client
        with sf() as s:
            _seed_f035(s)
        _login(client, "adm_audit")

        calls: list[dict] = []

        def fake_audit(request, *, action, entity_type, summary, **kwargs):
            calls.append(
                {
                    "action": action,
                    "entity_type": entity_type,
                    "summary": summary,
                    "kwargs": kwargs,
                }
            )

        monkeypatch.setattr(audit_module, "write_explicit_audit", fake_audit)
        res = client.get("/api/audit-logs/export")
        assert res.status_code == 200, res.text

        # 必須產生一筆 EXPORT / audit_log entity 的 audit
        assert any(
            c["entity_type"] == "audit_log" and c["action"] == "EXPORT" for c in calls
        ), f"expected audit_log EXPORT audit call, got {calls}"


# ─────────────────────────────────────────────────────────────────────────
# F-044：dismissal_calls cancel 需 originator 或管理角色
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f044_client(tmp_path):
    db_path = tmp_path / "f044.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(dismissal_calls_router)

    # mock WebSocket manager 避免實際廣播
    fake_manager = AsyncMock()
    fake_manager.broadcast = AsyncMock()
    dismissal_calls_module._get_manager = lambda: fake_manager

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f044(session) -> dict:
    """建立兩位 hr 角色（持 STUDENTS_WRITE）、admin、supervisor 帳號 +
    1 筆由 hr_a 建立的 dismissal call。"""
    classroom = Classroom(name="向日葵班", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S001",
        name="小明",
        classroom_id=classroom.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(student)
    session.flush()

    emp_a = _create_employee(session, "EA01", "原建立者")
    emp_b = _create_employee(session, "EB01", "他人")

    # 原建立者（custom role 持 STUDENTS_WRITE，非 admin/hr/supervisor）
    hr_a = _create_user(
        session,
        username="hr_a",
        role="staff",
        permissions=int(Permission.STUDENTS_WRITE) | int(Permission.STUDENTS_READ),
        employee_id=emp_a.id,
    )
    # 另一位 staff（同樣 custom role），不是建立者
    hr_b = _create_user(
        session,
        username="hr_b",
        role="staff",
        permissions=int(Permission.STUDENTS_WRITE) | int(Permission.STUDENTS_READ),
        employee_id=emp_b.id,
    )
    # admin：完整權限
    admin_u = _create_user(
        session,
        username="adm_d",
        role="admin",
        permissions=-1,
    )
    # supervisor：完整權限
    sup_u = _create_user(
        session,
        username="sup_d",
        role="supervisor",
        permissions=-1,
    )

    # 由 hr_a 發起的接送通知
    call = StudentDismissalCall(
        student_id=student.id,
        classroom_id=classroom.id,
        requested_by_user_id=hr_a.id,
        status="pending",
        requested_at=datetime.now(timezone.utc),
    )
    session.add(call)
    session.commit()
    session.refresh(call)

    return {
        "classroom": classroom,
        "student": student,
        "hr_a": hr_a,
        "hr_b": hr_b,
        "admin_u": admin_u,
        "sup_u": sup_u,
        "call_id": call.id,
    }


def _fresh_call_id(
    session_factory, classroom_id: int, student_id: int, user_id: int
) -> int:
    """建立新的 pending 接送通知（先把舊的清掉避免 409）。"""
    with session_factory() as s:
        # 把同學生既有 pending/acknowledged 改 cancelled
        for c in (
            s.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.student_id == student_id,
                StudentDismissalCall.status.in_(["pending", "acknowledged"]),
            )
            .all()
        ):
            c.status = "completed"
        s.flush()

        call = StudentDismissalCall(
            student_id=student_id,
            classroom_id=classroom_id,
            requested_by_user_id=user_id,
            status="pending",
            requested_at=datetime.now(timezone.utc),
        )
        s.add(call)
        s.commit()
        s.refresh(call)
        return call.id


class TestF044_DismissalCancel:
    """取消接送通知需為原建立者或管理角色。"""

    def test_originator_can_cancel_own_call(self, f044_client):
        client, sf = f044_client
        with sf() as s:
            data = _seed_f044(s)
        call_id = data["call_id"]

        _login(client, "hr_a")
        res = client.post(f"/api/dismissal-calls/{call_id}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled"

    def test_other_user_cannot_cancel_anothers_call(self, f044_client):
        client, sf = f044_client
        with sf() as s:
            data = _seed_f044(s)
        call_id = data["call_id"]

        _login(client, "hr_b")
        res = client.post(f"/api/dismissal-calls/{call_id}/cancel")
        assert res.status_code == 403, res.text

        # 確認 call 狀態未變
        with sf() as s:
            call = (
                s.query(StudentDismissalCall)
                .filter(StudentDismissalCall.id == call_id)
                .first()
            )
            assert call.status == "pending"

    def test_admin_can_cancel_anothers_call(self, f044_client):
        client, sf = f044_client
        with sf() as s:
            data = _seed_f044(s)
        call_id = data["call_id"]

        _login(client, "adm_d")
        res = client.post(f"/api/dismissal-calls/{call_id}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled"

    def test_supervisor_can_cancel_anothers_call(self, f044_client):
        client, sf = f044_client
        with sf() as s:
            data = _seed_f044(s)
        call_id = data["call_id"]

        _login(client, "sup_d")
        res = client.post(f"/api/dismissal-calls/{call_id}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled"
