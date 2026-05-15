"""T7（codebase review 2026-05-14）— 補加班 route 的 AuditLog 寫入斷言。

加班屬「會影響薪資的高敏感寫入」,既有 test_overtimes*.py 只覆蓋 helper / 業務邏輯,
從未驗證「成功建立 / 核准加班後,AuditLog 是否真的留下一筆稽核痕跡」。
若 audit_summary 被誤刪、middleware ENTITY_PATTERNS 漏配,測試會 silently pass。

涵蓋:
- POST /api/overtimes                → action="overtime_create"
- PUT  /api/overtimes/{id}/approve   → action="overtime_approve",含 risk_tag 標記
"""

import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.overtimes as overtimes_module
import models.base as base_module
import utils.audit as audit_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.overtimes import router as overtimes_router
from models.database import (
    AuditLog,
    Base,
    Employee,
    OvertimeRecord,
    User,
)
from utils.audit import AuditMiddleware
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "overtime-audit.sqlite"
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

    # 注入 mock SalaryEngine 避免核准/建立後實際觸發薪資重算
    fake_engine = MagicMock()
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_engine)

    # AuditMiddleware 預設用 asyncio.to_thread 背景寫入 audit_logs，測試會發生:
    # request 結束 → assert 立刻執行 → 背景 thread 尚未完成 → AuditLog 表為空。
    # 改為同步寫入消除此 race，讓 audit 斷言可被穩定驗證。
    monkeypatch.setattr(
        audit_module, "_schedule_audit_write", audit_module._write_audit_sync
    )

    app = FastAPI()
    # 必掛 AuditMiddleware,否則 audit_summary 永遠不會被 commit 進 audit_logs
    app.add_middleware(AuditMiddleware)
    app.include_router(auth_router)
    app.include_router(overtimes_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin_and_employee(session_factory):
    with session_factory() as s:
        emp = Employee(
            employee_id="OT001",
            name="加班測試員工",
            base_salary=36000,
            employee_type="regular",
            is_active=True,
        )
        s.add(emp)
        s.flush()
        admin = User(
            employee_id=None,
            username="ot_admin",
            password_hash=hash_password("AdminPass123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
        return emp.id


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "ot_admin", "password": "AdminPass123"},
    )


# ── POST /overtimes：建立加班應寫入 overtime_create AuditLog ────────────────


def test_create_overtime_writes_audit_row(app_client):
    client, sf = app_client
    emp_id = _seed_admin_and_employee(sf)
    assert _login(client).status_code == 200

    res = client.post(
        "/api/overtimes",
        json={
            "employee_id": emp_id,
            "overtime_date": "2026-05-15",
            "overtime_type": "weekday",
            "start_time": "18:00",
            "end_time": "20:00",
            "hours": 2.0,
            "reason": "audit 斷言測試用",
            "use_comp_leave": False,
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    ot_id = body["id"]

    with sf() as s:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.entity_type == "overtime", AuditLog.entity_id == str(ot_id)
            )
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert rows, "POST /overtimes 必須留下一筆 entity_type=overtime 的 AuditLog"
        summary = rows[0].summary or ""
        assert "建立加班" in summary, f"summary 缺『建立加班』語意：{summary!r}"
        assert (
            f"employee_id={emp_id}" in summary
        ), f"summary 缺 employee_id：{summary!r}"
        # changes 必含 action 標記,前端用 action 細分操作
        changes = rows[0].changes
        if isinstance(changes, str):
            changes = json.loads(changes)
        assert changes is not None, "changes 不應為 NULL"
        assert (
            changes.get("action") == "overtime_create"
        ), f"action 應為 overtime_create，實際 {changes.get('action')!r}"


# ── PUT /overtimes/{id}/approve：核准應寫入 overtime_approve AuditLog ─────


def test_approve_overtime_writes_audit_row(app_client):
    client, sf = app_client
    emp_id = _seed_admin_and_employee(sf)

    with sf() as s:
        ot = OvertimeRecord(
            employee_id=emp_id,
            overtime_date=date(2026, 5, 15),
            overtime_type="weekday",
            hours=2.0,
            overtime_pay=0,
            is_approved=None,
        )
        s.add(ot)
        s.commit()
        ot_id = ot.id

    assert _login(client).status_code == 200
    res = client.put(
        f"/api/overtimes/{ot_id}/approve",
        json={"approved": True},
    )
    assert res.status_code == 200, res.text

    with sf() as s:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.entity_type == "overtime", AuditLog.entity_id == str(ot_id)
            )
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert rows, "PUT /overtimes/{id}/approve 必須留下 AuditLog"
        summary = rows[0].summary or ""
        assert "核准加班" in summary, f"summary 應含『核准加班』：{summary!r}"
        changes = rows[0].changes
        if isinstance(changes, str):
            changes = json.loads(changes)
        assert changes is not None
        assert changes.get("action") == "overtime_approve", changes
        assert changes.get("decision") == "approved", changes


# ── 退審已核准加班 → audit summary 應帶 risk_tag ─────────────────────────


def test_reject_approved_overtime_carries_risk_tag(app_client):
    """已核准 → 駁回 是高風險操作,summary 必須打 ⚠ reject_of_approved 標記
    供 AuditLogView 篩選。"""
    client, sf = app_client
    emp_id = _seed_admin_and_employee(sf)

    with sf() as s:
        ot = OvertimeRecord(
            employee_id=emp_id,
            overtime_date=date(2026, 5, 15),
            overtime_type="weekday",
            hours=2.0,
            overtime_pay=540,
            is_approved=True,  # 已核准
        )
        s.add(ot)
        s.commit()
        ot_id = ot.id

    assert _login(client).status_code == 200
    res = client.put(
        f"/api/overtimes/{ot_id}/approve",
        json={"approved": False, "rejection_reason": "退審測試用"},
    )
    assert res.status_code == 200, res.text

    with sf() as s:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.entity_type == "overtime", AuditLog.entity_id == str(ot_id)
            )
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert rows
        summary = rows[0].summary or ""
        assert "駁回加班" in summary, f"summary 應含『駁回加班』：{summary!r}"
        assert (
            "reject_of_approved" in summary
        ), f"已核准 → 駁回必須打 reject_of_approved risk_tag：{summary!r}"
