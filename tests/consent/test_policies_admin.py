"""tests/consent/test_policies_admin.py — admin policy 版本管理端點整合測試。

覆蓋：
1. 無 DSR_MANAGE（teacher）→ GET/POST 403
2. 有 DSR_MANAGE：list 回已建 policy（order effective_at desc）
3. POST 建新版 → 200 + 回傳欄位完整
4. POST 重複 version → 409
5. 建新版後 has_signed_current_policy 行為：
   - seed 家長簽了舊版（service_essential） → 建新版 effective_at=now → False（重簽偵測）

P2-2 PDPA Phase 2 enforcement 補遺。
"""

from __future__ import annotations

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
from models.consent import (
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.database import Base, User
from utils.auth import create_access_token, hash_password
from utils.taipei_time import now_taipei_naive

# ============================================================
# Fixture：SQLite in-memory + TestClient
# ============================================================

OLD_EFFECTIVE = now_taipei_naive() - timedelta(days=1)
OLD_VERSION = "2025.1"


def _seed_db(session_factory):
    """seed admin + teacher（無 DSR_MANAGE）+ 一筆已生效舊版 policy。"""
    session = session_factory()
    admin = User(
        username="policy_admin",
        password_hash=hash_password("pass"),
        role="admin",
        permission_names=["*"],
    )
    teacher = User(
        username="policy_teacher",
        password_hash=hash_password("pass"),
        role="teacher",
        permission_names=["DASHBOARD"],
    )
    session.add(admin)
    session.add(teacher)
    session.flush()

    # 一筆已生效的舊版 policy（effective_at = 昨天）
    old_pv = PolicyVersion(
        version=OLD_VERSION,
        effective_at=OLD_EFFECTIVE,
        document_path="/policies/2025-1.pdf",
        summary="舊版摘要",
    )
    session.add(old_pv)
    session.commit()
    session.close()


@pytest.fixture
def policy_admin_client(tmp_path):
    db_path = tmp_path / "policy-admin.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    from api.policies_admin import router as policies_admin_router
    from api.auth import router as auth_router

    app = FastAPI()
    app.include_router(policies_admin_router)
    app.include_router(auth_router)

    _seed_db(session_factory)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _admin_login(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "policy_admin", "password": "pass"}
    )
    assert resp.status_code == 200


def _teacher_login(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "policy_teacher", "password": "pass"}
    )
    assert resp.status_code == 200


# ============================================================
# 403 — 無 DSR_MANAGE 的 teacher 被拒
# ============================================================


class TestPoliciesAdminBlocksTeacherEvenWithPermission:
    """GUARD-1：teacher 即使（誤配）持有 DSR_MANAGE 也不得存取政策版本管理端
    （require_staff_permission 的 role 結構閘）。"""

    def _login_teacher_with_dsr(self, c, sf) -> None:
        with sf() as session:
            session.add(
                User(
                    username="policy_teacher_priv",
                    password_hash=hash_password("pass"),
                    role="teacher",
                    permission_names=["DSR_MANAGE"],
                )
            )
            session.commit()
        resp = c.post(
            "/api/auth/login",
            json={"username": "policy_teacher_priv", "password": "pass"},
        )
        assert resp.status_code == 200

    def test_list_blocks_teacher_even_with_dsr_manage(self, policy_admin_client):
        c, sf = policy_admin_client
        self._login_teacher_with_dsr(c, sf)
        resp = c.get("/api/admin/policies")
        assert resp.status_code == 403

    def test_create_blocks_teacher_even_with_dsr_manage(self, policy_admin_client):
        c, sf = policy_admin_client
        self._login_teacher_with_dsr(c, sf)
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.9",
                "effective_at": now_taipei_naive().isoformat(),
                "document_path": "/policies/2026-9.pdf",
            },
        )
        assert resp.status_code == 403


class TestPoliciesAdmin403:
    def test_list_without_permission_returns_403(self, policy_admin_client):
        c, _ = policy_admin_client
        _teacher_login(c)
        resp = c.get("/api/admin/policies")
        assert resp.status_code == 403

    def test_post_without_permission_returns_403(self, policy_admin_client):
        c, _ = policy_admin_client
        _teacher_login(c)
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.1",
                "effective_at": now_taipei_naive().isoformat(),
                "document_path": "/policies/2026-1.pdf",
            },
        )
        assert resp.status_code == 403


# ============================================================
# GET "" — list 所有 PolicyVersion，order effective_at desc
# ============================================================


class TestPoliciesAdminList:
    def test_list_returns_existing_policy(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)
        resp = c.get("/api/admin/policies")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["version"] == OLD_VERSION

    def test_list_response_schema_fields(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)
        resp = c.get("/api/admin/policies")
        assert resp.status_code == 200
        item = resp.json()[0]
        required = {"id", "version", "effective_at", "document_path", "created_at"}
        assert required.issubset(item.keys())

    def test_list_order_effective_at_desc(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)

        # 建立第二筆（effective_at 更新）
        future_at = (now_taipei_naive() + timedelta(days=30)).isoformat()
        c.post(
            "/api/admin/policies",
            json={
                "version": "2026.2",
                "effective_at": future_at,
                "document_path": "/policies/2026-2.pdf",
            },
        )

        resp = c.get("/api/admin/policies")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        # effective_at desc：2026.2 排第一（更新）、2025.1 排第二（較舊）
        assert items[0]["version"] == "2026.2"
        assert items[1]["version"] == OLD_VERSION


# ============================================================
# POST "" — 建立新 PolicyVersion
# ============================================================


class TestPoliciesAdminCreate:
    def test_create_new_version_returns_200_with_fields(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)
        effective = now_taipei_naive().isoformat()
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.1",
                "effective_at": effective,
                "document_path": "/policies/2026-1.pdf",
                "summary": "2026 第一版隱私政策",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "2026.1"
        assert body["document_path"] == "/policies/2026-1.pdf"
        assert body["summary"] == "2026 第一版隱私政策"
        assert "id" in body
        assert "effective_at" in body
        assert "created_at" in body

    def test_create_without_summary_succeeds(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.1-no-summary",
                "effective_at": now_taipei_naive().isoformat(),
                "document_path": "/policies/2026-1-no-summary.pdf",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["summary"] is None

    def test_create_duplicate_version_returns_409(self, policy_admin_client):
        c, _ = policy_admin_client
        _admin_login(c)
        # 第一次建立 2025.1 已存在（seed）
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": OLD_VERSION,
                "effective_at": now_taipei_naive().isoformat(),
                "document_path": "/policies/dup.pdf",
            },
        )
        assert resp.status_code == 409

    def test_create_future_effective_at_allowed(self, policy_admin_client):
        """排程升版：effective_at 可以是未來時間。"""
        c, _ = policy_admin_client
        _admin_login(c)
        future = (now_taipei_naive() + timedelta(days=30)).isoformat()
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.future",
                "effective_at": future,
                "document_path": "/policies/future.pdf",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == "2026.future"


# ============================================================
# 建新版後 has_signed_current_policy 重簽偵測
# ============================================================


class TestPoliciesAdminResignDetection:
    def test_new_policy_triggers_resign_requirement(self, policy_admin_client):
        """seed 家長簽了舊版（service_essential） → 建新版 effective_at=now → has_signed_current_policy 回 False。

        驗證：建立新版後，即使家長對舊版簽了 service_essential，
        has_signed_current_policy(session, user_id) 應回 False（需重簽）。
        """
        from services.consent.checker import has_signed_current_policy

        c, session_factory = policy_admin_client
        session = session_factory()

        # 1. 取 seed 舊版 policy id
        old_pv = (
            session.query(PolicyVersion)
            .filter(PolicyVersion.version == OLD_VERSION)
            .first()
        )
        assert old_pv is not None

        # 2. 建立 parent user（非 admin / teacher）
        from utils.auth import hash_password as _hp

        parent_user = User(
            username="resign_parent",
            password_hash=_hp("pass"),
            role="parent",
            permission_names=[],
        )
        session.add(parent_user)
        session.flush()

        # 3. 家長對舊版簽 service_essential（consented=True）
        log = ParentConsentLog(
            user_id=parent_user.id,
            policy_version_id=old_pv.id,
            scope=CONSENT_SCOPE_SERVICE_ESSENTIAL,
            consented=True,
            consented_at=now_taipei_naive() - timedelta(hours=1),
        )
        session.add(log)
        session.commit()

        # 4. 驗證：此時 has_signed_current_policy 應為 True（簽的就是當前 policy）
        result_before = has_signed_current_policy(session, parent_user.id)
        assert result_before is True, "舊版為 current 時，已簽舊版 → True"

        # 5. 透過 API 建立新版（effective_at=now，即立刻生效）
        _admin_login(c)
        new_effective = now_taipei_naive().isoformat()
        resp = c.post(
            "/api/admin/policies",
            json={
                "version": "2026.1-resign",
                "effective_at": new_effective,
                "document_path": "/policies/2026-1-resign.pdf",
                "summary": "觸發重簽測試",
            },
        )
        assert resp.status_code == 200

        # 6. 建新版後：has_signed_current_policy 對家長 → False（最新版未簽）
        result_after = has_signed_current_policy(session, parent_user.id)
        assert (
            result_after is False
        ), "建立新版後 current policy 改變，家長舊版簽署不再有效，應回 False"

        session.close()
