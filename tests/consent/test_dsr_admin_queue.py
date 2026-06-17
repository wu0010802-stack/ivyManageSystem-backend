"""api/dsr_admin.py 整合測試 — DSR_MANAGE 權限 + admin DSR queue。

Task 10：DSR_MANAGE 權限存在於 enum + PERMISSION_LABELS + admin wildcard 含。
Task 11：list/reject/approve-status 三個端點。approve 只做 status 更新（Task 12 尚未實作）。

P2-3 PDPA Phase 2 enforcement。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from models.database import Base, User
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_STATUS_PENDING,
    DsrRequest,
)
from utils.auth import create_access_token, hash_password
from utils.permissions import PERMISSION_LABELS, ROLE_TEMPLATES, Permission, WILDCARD

# ============================================================
# Task 10：DSR_MANAGE 權限單元斷言
# ============================================================


def test_dsr_manage_enum_value():
    """Task 10：Permission.DSR_MANAGE.value == "DSR_MANAGE"。"""
    assert Permission.DSR_MANAGE.value == "DSR_MANAGE"


def test_dsr_manage_in_permission_labels():
    """Task 10：PERMISSION_LABELS 應含 DSR_MANAGE 條目。"""
    assert "DSR_MANAGE" in PERMISSION_LABELS


def test_admin_role_template_covers_dsr_manage():
    """Task 10：admin 走 WILDCARD，has_permission(["*"], DSR_MANAGE) 必須為 True。"""
    from utils.permissions import has_permission

    admin_perms = ROLE_TEMPLATES["admin"]
    assert WILDCARD in admin_perms  # admin 角色為 ["*"]
    assert has_permission(admin_perms, Permission.DSR_MANAGE) is True


# ============================================================
# Fixture：SQLite in-memory + TestClient
# ============================================================


def _seed_db(session_factory):
    """seed admin + teacher（無 DSR_MANAGE）+ 幾筆 DsrRequest。"""
    session = session_factory()
    admin = User(
        username="dsr_admin",
        password_hash=hash_password("pass"),
        role="admin",
        permission_names=["*"],
    )
    teacher = User(
        username="dsr_teacher",
        password_hash=hash_password("pass"),
        role="teacher",
        permission_names=["DASHBOARD"],
    )
    session.add(admin)
    session.add(teacher)
    session.flush()  # 取得 id

    # 建立幾筆 pending DSR（user_id = admin.id，FK 滿足）。
    # Task 12 升級後 delete DSR 會做 ownership 重驗（Guardian 存在才能過）。
    # 此處 seed 全用 correct 型別：Task 11 只測 status 更新行為，
    # delete 分派測試在 tests/consent/test_dsr_approve_execute.py 補齊。
    for i in range(3):
        session.add(
            DsrRequest(
                user_id=admin.id,
                request_type=DSR_REQUEST_TYPE_CORRECT,
                status=DSR_STATUS_PENDING,
                subject_entity_type="student",
                subject_entity_id=i + 1,
                field_name="name",
                new_value=f"test value {i}",
                reason=f"test reason {i}",
            )
        )
    session.commit()
    session.close()


@pytest.fixture
def dsr_admin_client(tmp_path):
    db_path = tmp_path / "dsr-admin.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    from api.dsr_admin import router as dsr_admin_router
    from api.auth import router as auth_router

    app = FastAPI()
    app.include_router(dsr_admin_router)
    app.include_router(auth_router)

    _seed_db(session_factory)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _admin_login(client) -> dict:
    resp = client.post(
        "/api/auth/login", json={"username": "dsr_admin", "password": "pass"}
    )
    assert resp.status_code == 200
    return resp


def _teacher_login(client) -> dict:
    resp = client.post(
        "/api/auth/login", json={"username": "dsr_teacher", "password": "pass"}
    )
    assert resp.status_code == 200
    return resp


def _get_first_pending_id(client) -> int:
    """取得第一筆 pending DSR 的 id（admin 已登入）。"""
    resp = client.get("/api/admin/dsr-requests")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) > 0
    return items[0]["id"]


# ============================================================
# Task 11：403 — 無 DSR_MANAGE 權限的 teacher 被拒
# ============================================================


class TestDsrAdminQueue403:
    def test_list_without_permission_returns_403(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _teacher_login(c)
        resp = c.get("/api/admin/dsr-requests")
        assert resp.status_code == 403

    def test_reject_without_permission_returns_403(self, dsr_admin_client):
        c, sf = dsr_admin_client
        # 先用 admin 取得一筆 id，再換 teacher session 打
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        _teacher_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/reject", json={"decision_note": "拒絕"}
        )
        assert resp.status_code == 403

    def test_approve_without_permission_returns_403(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        _teacher_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/approve",
            json={"decision_note": "核准"},
        )
        assert resp.status_code == 403


class TestDsrAdminBlocksTeacherEvenWithPermission:
    """GUARD-1：teacher 即使（誤配）持有 DSR_MANAGE 也不得直接存取管理端 DSR API。
    require_staff_permission 的 role 結構閘（縱深防禦）——個資刪除/駁回核准不該因
    自訂角色或誤配把 DSR_MANAGE 給了教師就被觸及。"""

    def _login_teacher_with_dsr(self, c, sf) -> None:
        with sf() as session:
            session.add(
                User(
                    username="dsr_teacher_priv",
                    password_hash=hash_password("pass"),
                    role="teacher",
                    permission_names=["DSR_MANAGE"],
                )
            )
            session.commit()
        resp = c.post(
            "/api/auth/login",
            json={"username": "dsr_teacher_priv", "password": "pass"},
        )
        assert resp.status_code == 200

    def test_list_blocks_teacher_even_with_dsr_manage(self, dsr_admin_client):
        c, sf = dsr_admin_client
        self._login_teacher_with_dsr(c, sf)
        resp = c.get("/api/admin/dsr-requests")
        assert resp.status_code == 403

    def test_reject_blocks_teacher_even_with_dsr_manage(self, dsr_admin_client):
        c, sf = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        self._login_teacher_with_dsr(c, sf)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/reject",
            json={"decision_note": "x"},
        )
        assert resp.status_code == 403

    def test_approve_blocks_teacher_even_with_dsr_manage(self, dsr_admin_client):
        c, sf = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        self._login_teacher_with_dsr(c, sf)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/approve",
            json={"decision_note": "x"},
        )
        assert resp.status_code == 403


# ============================================================
# Task 11：有 DSR_MANAGE — 正常路徑
# ============================================================


class TestDsrAdminListEndpoint:
    def test_list_returns_pending_rows(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        resp = c.get("/api/admin/dsr-requests")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 3
        # 全為 pending（seed 均為 pending）
        for item in items:
            assert item["status"] == "pending"

    def test_list_filter_by_status(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        # 過濾 status=pending
        resp = c.get("/api/admin/dsr-requests?status=pending")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

        # 過濾 status=approved（尚無）
        resp = c.get("/api/admin/dsr-requests?status=approved")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_list_order_submitted_at_desc(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        resp = c.get("/api/admin/dsr-requests")
        assert resp.status_code == 200
        items = resp.json()
        ids = [item["id"] for item in items]
        # submitted_at desc → id 越大排越前（同秒則 id 降序）
        assert ids == sorted(ids, reverse=True)

    def test_list_response_schema(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        resp = c.get("/api/admin/dsr-requests")
        assert resp.status_code == 200
        item = resp.json()[0]
        required_fields = {"id", "user_id", "request_type", "status", "submitted_at"}
        assert required_fields.issubset(item.keys())


class TestDsrAdminRejectEndpoint:
    def test_reject_pending_returns_rejected_status(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/reject",
            json={"decision_note": "不符申請條件"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["decision_note"] == "不符申請條件"
        assert body["decided_at"] is not None
        assert body["decided_by"] is not None

    def test_reject_non_pending_returns_404(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        # 先 reject
        c.post(
            f"/api/admin/dsr-requests/{first_id}/reject",
            json={"decision_note": "第一次拒絕"},
        )
        # 再次 reject 應 404（已非 pending）
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/reject",
            json={"decision_note": "第二次嘗試"},
        )
        assert resp.status_code == 404

    def test_reject_nonexistent_id_returns_404(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        resp = c.post(
            "/api/admin/dsr-requests/99999/reject",
            json={"decision_note": "不存在"},
        )
        assert resp.status_code == 404


class TestDsrAdminApproveEndpoint:
    def test_approve_pending_returns_approved_status(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/approve",
            json={"decision_note": "核准刪除"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["decision_note"] == "核准刪除"
        assert body["decided_at"] is not None
        assert body["decided_by"] is not None

    def test_approve_non_pending_returns_404(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        first_id = _get_first_pending_id(c)
        # 先 approve
        c.post(
            f"/api/admin/dsr-requests/{first_id}/approve",
            json={"decision_note": "第一次核准"},
        )
        # 再次 approve 應 404
        resp = c.post(
            f"/api/admin/dsr-requests/{first_id}/approve",
            json={"decision_note": "第二次嘗試"},
        )
        assert resp.status_code == 404

    def test_approve_nonexistent_id_returns_404(self, dsr_admin_client):
        c, _ = dsr_admin_client
        _admin_login(c)
        resp = c.post(
            "/api/admin/dsr-requests/99999/approve",
            json={"decision_note": "不存在"},
        )
        assert resp.status_code == 404
