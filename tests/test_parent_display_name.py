"""家長端顯示名（display_name）解析與 LIFF 同步測試。

涵蓋：
- resolve_parent_display_name 優先序（display_name → primary Guardian.name → earliest → "家長"）
- /api/parent/home/summary me.name 不再回 parent_line_<id>
- /api/parent/me me.name 走 helper
- LIFF 登入既有 user 同步 LINE displayName
- LIFF /bind 流程把 LINE displayName 寫入新建 User
"""

import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.parent_portal import (
    init_parent_line_service,
    parent_router as parent_portal_router,
)
from api.parent_portal._shared import resolve_parent_display_name
from api.parent_portal.auth import _bind_failures, _hash_code
from api.parent_portal.home import _home_summary_cache
from models.database import (
    Base,
    Guardian,
    GuardianBindingCode,
    Student,
    User,
)
from utils.auth import create_access_token


class FakeLineLoginService:
    def __init__(self, sub_to_name=None):
        self.sub_to_name = dict(sub_to_name or {})

    def is_configured(self):
        return True

    def verify_id_token(self, id_token: str) -> dict:
        if id_token in self.sub_to_name:
            sub, name = self.sub_to_name[id_token]
            return {"sub": sub, "aud": "test", "name": name}
        raise HTTPException(status_code=401, detail="invalid id_token")


@pytest.fixture
def parent_app(tmp_path):
    db_path = tmp_path / "parent-display.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = factory
    Base.metadata.create_all(engine)

    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    _home_summary_cache.clear()

    fake_line = FakeLineLoginService(
        {
            "token-existing-parent": ("U_existing_001", "陳爸爸"),
            "token-new-parent-2": ("U_new_001", "新家長 LINE 暱稱"),
            "token-blank-name-3": ("U_blank_001", "   "),
        }
    )
    init_parent_line_service(fake_line)

    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, factory, fake_line

    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    _home_summary_cache.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


# ── helpers ─────────────────────────────────────────────────────────────


def _make_parent_user(session, *, line_user_id="U1", display_name=None) -> User:
    u = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        display_name=display_name,
        token_version=0,
    )
    session.add(u)
    session.flush()
    return u


def _make_student_with_guardian(
    session, *, user, name="小華", guardian_name="王媽媽", is_primary=True
) -> Student:
    s = Student(student_id=f"S_{name}", name=name, is_active=True)
    session.add(s)
    session.flush()
    g = Guardian(
        student_id=s.id,
        user_id=user.id,
        name=guardian_name,
        relation="母親",
        is_primary=is_primary,
    )
    session.add(g)
    session.flush()
    return s


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


# ── helper unit tests ───────────────────────────────────────────────────


class TestResolveParentDisplayName:
    def test_returns_display_name_when_set(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name="LINE 暱稱阿明")
            session.commit()
            assert resolve_parent_display_name(session, u) == "LINE 暱稱阿明"

    def test_falls_back_to_primary_guardian_name(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            # 兩筆 guardian：一筆 primary、一筆非 primary（不同名字）
            _make_student_with_guardian(
                session,
                user=u,
                name="A",
                guardian_name="非主聯絡人",
                is_primary=False,
            )
            _make_student_with_guardian(
                session,
                user=u,
                name="B",
                guardian_name="主聯絡人媽媽",
                is_primary=True,
            )
            session.commit()
            assert resolve_parent_display_name(session, u) == "主聯絡人媽媽"

    def test_falls_back_to_earliest_guardian_when_no_primary(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            # 都不是 primary → 取最早建立那筆
            first = _make_student_with_guardian(
                session,
                user=u,
                name="E1",
                guardian_name="先建檔的家長",
                is_primary=False,
            )
            assert first  # noqa: F841
            _make_student_with_guardian(
                session,
                user=u,
                name="E2",
                guardian_name="後建檔的家長",
                is_primary=False,
            )
            session.commit()
            assert resolve_parent_display_name(session, u) == "先建檔的家長"

    def test_falls_back_to_constant_when_no_guardian(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            session.commit()
            assert resolve_parent_display_name(session, u) == "家長"

    def test_blank_display_name_treated_as_unset(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name="   ")
            _make_student_with_guardian(session, user=u, guardian_name="爸爸")
            session.commit()
            assert resolve_parent_display_name(session, u) == "爸爸"

    def test_skips_soft_deleted_guardian(self, parent_app):
        _, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            # 被軟刪除的 guardian 不算
            student = Student(student_id="SX", name="X", is_active=True)
            session.add(student)
            session.flush()
            session.add(
                Guardian(
                    student_id=student.id,
                    user_id=u.id,
                    name="刪除的家長",
                    relation="父親",
                    is_primary=True,
                    deleted_at=datetime.now(),
                )
            )
            session.flush()
            session.commit()
            assert resolve_parent_display_name(session, u) == "家長"


# ── API integration tests ───────────────────────────────────────────────


class TestApiResponses:
    def test_home_summary_uses_display_name_priority(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name="阿明爸")
            _make_student_with_guardian(session, user=u, guardian_name="王媽")
            session.commit()
            user_id = u.id

        with factory() as session:
            u = session.query(User).get(user_id)
            token = _parent_token(u)
        resp = client.get(
            "/api/parent/home/summary",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        me = resp.json()["me"]
        assert me["name"] == "阿明爸"  # display_name 勝
        assert "parent_line_" not in me["name"]

    def test_home_summary_falls_back_to_guardian_name(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            _make_student_with_guardian(session, user=u, guardian_name="王媽")
            session.commit()
            token = _parent_token(u)

        resp = client.get(
            "/api/parent/home/summary",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["me"]["name"] == "王媽"

    def test_me_endpoint_never_returns_username_id(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            u = _make_parent_user(session, display_name=None)
            session.commit()
            token = _parent_token(u)

        resp = client.get("/api/parent/me", cookies={"access_token": token})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "家長"
        assert "parent_line_" not in body["name"]


# ── LIFF login 同步 display_name ────────────────────────────────────────


class TestLiffSyncDisplayName:
    def test_existing_user_gets_display_name_synced_on_login(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            _make_parent_user(session, line_user_id="U_existing_001", display_name=None)
            session.commit()

        resp = client.post(
            "/api/parent/auth/liff-login", json={"id_token": "token-existing-parent"}
        )
        assert resp.status_code == 200
        # API 回應裡的 name 是 helper 結果（display_name 剛被同步寫入）
        assert resp.json()["user"]["name"] == "陳爸爸"

        with factory() as session:
            u = (
                session.query(User)
                .filter(User.line_user_id == "U_existing_001")
                .first()
            )
            assert u.display_name == "陳爸爸"

    def test_existing_user_blank_line_name_does_not_overwrite(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            _make_parent_user(
                session,
                line_user_id="U_blank_001",
                display_name="原本的名字",
            )
            session.commit()

        resp = client.post(
            "/api/parent/auth/liff-login", json={"id_token": "token-blank-name-3"}
        )
        assert resp.status_code == 200
        with factory() as session:
            u = session.query(User).filter(User.line_user_id == "U_blank_001").first()
            # 全空白被清成 None → 不覆寫
            assert u.display_name == "原本的名字"

    def test_bind_first_child_writes_display_name_from_temp_token(self, parent_app):
        client, factory, _ = parent_app
        with factory() as session:
            admin = User(
                username="admin_for_binding",
                password_hash="x",
                role="admin",
                permissions=-1,
                is_active=True,
                token_version=0,
            )
            session.add(admin)
            session.flush()
            student = Student(student_id="SS", name="新生", is_active=True)
            session.add(student)
            session.flush()
            guardian = Guardian(
                student_id=student.id,
                name="行政建檔家長",
                relation="父親",
                is_primary=True,
            )
            session.add(guardian)
            session.flush()
            plain = "BIND0001"
            from datetime import timedelta as _td

            session.add(
                GuardianBindingCode(
                    guardian_id=guardian.id,
                    code_hash=_hash_code(plain),
                    expires_at=datetime.now() + _td(hours=1),
                    used_at=None,
                    used_by_user_id=None,
                    created_by=admin.id,
                )
            )
            session.commit()

        # 1) LIFF login → need_binding（temp_token 帶 line displayName）
        liff = client.post(
            "/api/parent/auth/liff-login", json={"id_token": "token-new-parent-2"}
        )
        assert liff.status_code == 200
        assert liff.json()["status"] == "need_binding"
        assert "parent_bind_token" in liff.cookies

        # 2) /bind 完成綁定（用 client cookies 自動帶 temp_token）
        bind = client.post(
            "/api/parent/auth/bind",
            json={"code": plain},
            cookies={"parent_bind_token": liff.cookies["parent_bind_token"]},
        )
        assert bind.status_code == 200
        assert bind.json()["user"]["name"] == "新家長 LINE 暱稱"

        with factory() as session:
            u = session.query(User).filter(User.line_user_id == "U_new_001").first()
            assert u is not None
            assert u.display_name == "新家長 LINE 暱稱"
