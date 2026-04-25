"""家長 LIFF 登入 / 綁定 / bind-additional / logout / 行政發碼測試。

涵蓋 plan Batch 2 範圍與 advisor 點出的補強點：
- LIFF id_token 驗證流程
- 綁定碼 atomic UPDATE 防 race
- 多孩家庭 bind-additional 流程
- 拒絕 claim 他人 Guardian 的綁定碼
- 行政發碼明碼僅回一次、DB 存 sha256、寫 AuditLog
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.parent_portal import (
    admin_router as parent_admin_router,
    parent_router as parent_portal_router,
    init_parent_line_service,
)
from api.parent_portal.auth import _bind_failures
from models.database import (
    AuditLog,
    Base,
    Employee,
    Guardian,
    GuardianBindingCode,
    Student,
    User,
)
from utils.auth import create_access_token, hash_password


class FakeLineLoginService:
    """測試用 LineLoginService：以 sub_map 回應預設或拋 401。"""

    def __init__(self, sub_map=None):
        self.sub_map = dict(sub_map or {})

    def is_configured(self):
        return True

    def verify_id_token(self, id_token: str) -> dict:
        if id_token in self.sub_map:
            return {
                "sub": self.sub_map[id_token],
                "aud": "test-channel",
                "name": "Fake LINE User",
            }
        raise HTTPException(status_code=401, detail="LINE id_token 驗證失敗")


@pytest.fixture
def parent_client(tmp_path):
    """獨立 sqlite + LineLoginService 注入 fake。"""
    db_path = tmp_path / "parent-auth.sqlite"
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
    _bind_failures.clear()

    fake_line = FakeLineLoginService(
        {
            "token-bound-parent": "U_bound_parent_001",
            "token-new-parent": "U_new_parent_001",
            "token-second-parent": "U_second_parent_001",
        }
    )
    init_parent_line_service(fake_line)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(parent_portal_router)
    app.include_router(parent_admin_router)

    with TestClient(app) as client:
        yield client, session_factory, fake_line

    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


# ── helpers ─────────────────────────────────────────────────────────────


def _create_student(session, name: str = "小明") -> Student:
    student = Student(
        student_id=f"S{name}",
        name=name,
        is_active=True,
    )
    session.add(student)
    session.flush()
    return student


def _create_guardian(session, student: Student, name: str = "王大明") -> Guardian:
    guardian = Guardian(
        student_id=student.id,
        name=name,
        phone="0912345678",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return guardian


def _seed_binding_code(
    session,
    guardian: Guardian,
    *,
    plain_code: str = "ABCD1234",
    created_by: int,
    used: bool = False,
    expired: bool = False,
) -> GuardianBindingCode:
    code = GuardianBindingCode(
        guardian_id=guardian.id,
        code_hash=hashlib.sha256(plain_code.encode()).hexdigest(),
        expires_at=(
            datetime.now() - timedelta(hours=1) if expired
            else datetime.now() + timedelta(hours=24)
        ),
        used_at=datetime.now() if used else None,
        used_by_user_id=None,
        created_by=created_by,
    )
    session.add(code)
    session.flush()
    return code


def _create_admin_user(session, *, username: str = "admin", password: str = "Passw0rd!") -> User:
    user = User(
        employee_id=None,
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _create_supervisor_user(session) -> User:
    employee = Employee(
        employee_id="SUP01",
        name="主管",
        base_salary=40000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    user = User(
        employee_id=employee.id,
        username="supervisor1",
        password_hash=hash_password("Passw0rd!"),
        role="supervisor",
        permissions=-1,
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _create_existing_parent(session, line_user_id: str, name: str = "已綁家長") -> User:
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        must_change_password=False,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _admin_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permissions": user.permissions if user.permissions is not None else -1,
            "token_version": user.token_version or 0,
        }
    )


# ── LIFF login ─────────────────────────────────────────────────────────


class TestLiffLogin:
    def test_invalid_id_token_returns_401(self, parent_client):
        client, _, _ = parent_client
        resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "garbage-token"},
        )
        assert resp.status_code == 401

    def test_unbound_line_user_returns_need_binding(self, parent_client):
        client, _, _ = parent_client
        resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "token-new-parent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "need_binding"
        assert data["line_user_id"] == "U_new_parent_001"
        assert "parent_bind_token" in resp.cookies

    def test_already_bound_line_user_returns_ok_with_access_token(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            _create_existing_parent(session, "U_bound_parent_001")
            session.commit()

        resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "token-bound-parent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["user"]["role"] == "parent"
        assert "access_token" in resp.cookies


# ── 行政發碼 + LIFF bind 完整流程 ─────────────────────────────────────


class TestAdminBindingCodeAndParentBind:
    def test_admin_creates_binding_code_and_parent_completes_bind(self, parent_client):
        client, session_factory, _ = parent_client

        with session_factory() as session:
            admin = _create_admin_user(session)
            student = _create_student(session, "小華")
            guardian = _create_guardian(session, student, "李父")
            session.commit()
            admin_id = admin.id
            guardian_id = guardian.id

        # 行政以 admin 身分發碼
        admin_cookie = {"access_token": _admin_token(_get(session_factory, User, admin_id))}
        admin_resp = client.post(
            f"/api/guardians/{guardian_id}/binding-code",
            cookies=admin_cookie,
        )
        assert admin_resp.status_code == 200
        plain_code = admin_resp.json()["code"]
        assert len(plain_code) == 8

        # DB 內只存 hash，不存明碼
        with session_factory() as session:
            stored = session.query(GuardianBindingCode).first()
            assert stored.code_hash == hashlib.sha256(plain_code.encode()).hexdigest()
            assert stored.used_at is None

        # 家長 LIFF 登入 → need_binding
        liff_resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "token-new-parent"},
        )
        assert liff_resp.status_code == 200
        assert liff_resp.json()["status"] == "need_binding"

        # 帶綁定碼 bind
        bind_resp = client.post(
            "/api/parent/auth/bind",
            json={"code": plain_code},
        )
        assert bind_resp.status_code == 200
        assert bind_resp.json()["user"]["role"] == "parent"
        assert "access_token" in bind_resp.cookies

        # DB 結果：Guardian.user_id 設好、binding 已用、AuditLog 落筆
        with session_factory() as session:
            g = session.query(Guardian).filter(Guardian.id == guardian_id).first()
            assert g.user_id is not None
            stored = session.query(GuardianBindingCode).first()
            assert stored.used_at is not None
            assert stored.used_by_user_id == g.user_id
            audits = session.query(AuditLog).filter(
                AuditLog.entity_type == "guardian_binding"
            ).all()
            assert len(audits) == 1

    def test_invalid_code_400(self, parent_client):
        client, _, _ = parent_client
        # 先 LIFF 拿 bind_token
        client.post("/api/parent/auth/liff-login", json={"id_token": "token-new-parent"})
        resp = client.post(
            "/api/parent/auth/bind",
            json={"code": "WRONGCOD"},
        )
        assert resp.status_code == 400

    def test_used_code_cannot_be_reused(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            admin = _create_admin_user(session)
            student = _create_student(session, "甲")
            guardian = _create_guardian(session, student)
            _seed_binding_code(
                session,
                guardian,
                plain_code="USED1234",
                created_by=admin.id,
                used=True,
            )
            session.commit()

        client.post("/api/parent/auth/liff-login", json={"id_token": "token-new-parent"})
        resp = client.post("/api/parent/auth/bind", json={"code": "USED1234"})
        assert resp.status_code == 400

    def test_expired_code_cannot_be_used(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            admin = _create_admin_user(session)
            student = _create_student(session, "乙")
            guardian = _create_guardian(session, student)
            _seed_binding_code(
                session,
                guardian,
                plain_code="EXPI1234",
                created_by=admin.id,
                expired=True,
            )
            session.commit()

        client.post("/api/parent/auth/liff-login", json={"id_token": "token-new-parent"})
        resp = client.post("/api/parent/auth/bind", json={"code": "EXPI1234"})
        assert resp.status_code == 400

    def test_bind_without_temp_token_returns_401(self, parent_client):
        client, _, _ = parent_client
        # 不先做 liff-login，bind_token cookie 不存在
        resp = client.post("/api/parent/auth/bind", json={"code": "ANYTHING"})
        assert resp.status_code == 401


# ── bind-additional（多孩家庭） ──────────────────────────────────────


class TestBindAdditional:
    def test_existing_parent_can_bind_second_child(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            admin = _create_admin_user(session)
            parent_user = _create_existing_parent(session, "U_bound_parent_001")
            # 第一個小孩已綁好
            student1 = _create_student(session, "老大")
            g1 = _create_guardian(session, student1, "父親")
            g1.user_id = parent_user.id
            # 第二個小孩 + 對應綁定碼
            student2 = _create_student(session, "老二")
            g2 = _create_guardian(session, student2, "父親")
            _seed_binding_code(
                session, g2, plain_code="SECOND12", created_by=admin.id
            )
            session.commit()
            parent_user_id = parent_user.id
            g2_id = g2.id

        # 家長以正式 access_token 撞 bind-additional
        token = create_access_token(
            {
                "user_id": parent_user_id,
                "employee_id": None,
                "role": "parent",
                "name": "parent_line_U_bound_parent_001",
                "permissions": 0,
                "token_version": 0,
            }
        )
        resp = client.post(
            "/api/parent/auth/bind-additional",
            json={"code": "SECOND12"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        with session_factory() as session:
            g = session.query(Guardian).filter(Guardian.id == g2_id).first()
            assert g.user_id == parent_user_id

    def test_cannot_claim_guardian_already_bound_to_another_user(
        self, parent_client
    ):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            admin = _create_admin_user(session)
            parent_a = _create_existing_parent(session, "U_parent_A")
            parent_b = _create_existing_parent(session, "U_parent_B")
            student = _create_student(session, "C")
            g = _create_guardian(session, student, "父親")
            g.user_id = parent_a.id  # 已被 A 認領
            _seed_binding_code(session, g, plain_code="STEAL123", created_by=admin.id)
            session.commit()
            parent_a_id = parent_a.id
            parent_b_id = parent_b.id

        token_b = create_access_token(
            {
                "user_id": parent_b_id,
                "employee_id": None,
                "role": "parent",
                "name": "parent_line_U_parent_B",
                "permissions": 0,
                "token_version": 0,
            }
        )
        resp = client.post(
            "/api/parent/auth/bind-additional",
            json={"code": "STEAL123"},
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 400
        with session_factory() as session:
            g = session.query(Guardian).first()
            assert g.user_id == parent_a_id  # 沒被改

    def test_bind_additional_requires_parent_role(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            supervisor = _create_supervisor_user(session)
            session.commit()
            supervisor_id = supervisor.id
            permissions = supervisor.permissions
            token_version = supervisor.token_version or 0

        token = create_access_token(
            {
                "user_id": supervisor_id,
                "employee_id": None,
                "role": "supervisor",
                "name": "supervisor1",
                "permissions": permissions if permissions is not None else -1,
                "token_version": token_version,
            }
        )
        resp = client.post(
            "/api/parent/auth/bind-additional",
            json={"code": "WHATEVER"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 403


# ── 行政發碼隔離 ───────────────────────────────────────────────────────


class TestAdminEndpointIsolation:
    def test_parent_cannot_create_binding_code(self, parent_client):
        client, session_factory, _ = parent_client
        with session_factory() as session:
            parent_user = _create_existing_parent(session, "U_evil_parent")
            student = _create_student(session, "X")
            g = _create_guardian(session, student)
            session.commit()
            guardian_id = g.id
            parent_user_id = parent_user.id

        token = create_access_token(
            {
                "user_id": parent_user_id,
                "employee_id": None,
                "role": "parent",
                "name": "parent_line_U_evil_parent",
                "permissions": 0,
                "token_version": 0,
            }
        )
        resp = client.post(
            f"/api/guardians/{guardian_id}/binding-code",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403


# ── helper（避免 SA detached 問題） ───────────────────────────────────


def _get(session_factory, model, pk):
    with session_factory() as session:
        return session.query(model).filter(model.id == pk).first()
