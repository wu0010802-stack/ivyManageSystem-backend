"""F-001 回歸測試：parent_portal/auth.py `bind` 缺 guardian.user_id 守衛

修補目標：在 `POST /api/parent/auth/bind` 的 atomic UPDATE 成功後、設定
`guardian.user_id` 之前，補上「若 guardian 已被他人綁定則拒絕」守衛
（對齊既有 `bind-additional` line 414 idiom）。

威脅：若行政發出的綁定碼外洩（紙本、訊息攔截、家長截圖外傳），持碼者
以另一個 LINE 帳號呼叫 `/auth/bind` 即可覆寫合法家長綁定，奪取該家庭
PII 並把原家長踢出系統。

涵蓋：
- 已被他人綁定 → 400 + Guardian.user_id 不被覆寫
- 首次綁定（guardian.user_id IS NULL）→ 200
- 持碼者 user 自己重綁的場景由綁定碼 single-use 阻擋（已驗證
  `_claim_binding_code_atomic` 的 `WHERE used_at IS NULL` 會先 400）；故
  該情境不於此檔測。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.parent_portal import (
    admin_router as parent_admin_router,
    init_parent_line_service,
    parent_router as parent_portal_router,
)
from api.parent_portal.auth import _bind_failures
from models.database import (
    Base,
    Guardian,
    GuardianBindingCode,
    Student,
    User,
)
from utils.auth import hash_password


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
    db_path = tmp_path / "parent-bind-takeover.sqlite"
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
    _bind_failures.clear()

    fake_line = FakeLineLoginService(
        {
            "token-victim-parent": "U_victim_parent_001",
            "token-attacker-parent": "U_attacker_parent_001",
            "token-fresh-parent": "U_fresh_parent_001",
        }
    )
    init_parent_line_service(fake_line)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(parent_portal_router)
    app.include_router(parent_admin_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ── helpers ─────────────────────────────────────────────────────────────


def _create_student(session, name: str) -> Student:
    student = Student(student_id=f"S_{name}", name=name, is_active=True)
    session.add(student)
    session.flush()
    return student


def _create_guardian(session, student: Student, name: str = "監護人") -> Guardian:
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


def _create_parent_user(session, line_user_id: str) -> User:
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


def _create_admin_user(session) -> User:
    user = User(
        employee_id=None,
        username="bind_admin",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _seed_binding_code(
    session,
    guardian: Guardian,
    *,
    plain_code: str,
    created_by: int,
) -> GuardianBindingCode:
    code = GuardianBindingCode(
        guardian_id=guardian.id,
        code_hash=hashlib.sha256(plain_code.encode()).hexdigest(),
        expires_at=datetime.now() + timedelta(hours=24),
        used_at=None,
        used_by_user_id=None,
        created_by=created_by,
    )
    session.add(code)
    session.flush()
    return code


# ── F-001 主要測試 ──────────────────────────────────────────────────────


class TestF001BindCannotOverwriteExistingBinding:
    """F-001：bind 必須拒絕覆寫已被他人綁定的 Guardian。"""

    def test_attacker_with_leaked_code_cannot_take_over_existing_guardian(
        self, parent_client
    ):
        """已被合法家長 A 綁定的 Guardian，攻擊者 B 持外洩綁定碼呼叫 /bind
        → 必須 400，且 Guardian.user_id 不被覆寫（仍為 A）。"""
        client, session_factory = parent_client

        with session_factory() as session:
            admin = _create_admin_user(session)
            victim_parent = _create_parent_user(session, "U_victim_parent_001")
            student = _create_student(session, "受害學生")
            guardian = _create_guardian(session, student, "受害家長")
            # Guardian 已綁定家長 A
            guardian.user_id = victim_parent.id
            # 同時配發一張綁定碼（情境：行政剛發碼，家長 A 已先用其他碼綁好；
            # 後來這張碼外洩給攻擊者）
            _seed_binding_code(
                session, guardian, plain_code="LEAKED12", created_by=admin.id
            )
            session.commit()
            victim_parent_id = victim_parent.id
            guardian_id = guardian.id

        # 攻擊者 B 用不同 LINE 帳號 LIFF 登入 → need_binding（拿到 bind temp token）
        liff_resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "token-attacker-parent"},
        )
        assert liff_resp.status_code == 200
        assert liff_resp.json()["status"] == "need_binding"

        # 攻擊者拿外洩的碼 bind → 應被 400 擋下
        bind_resp = client.post(
            "/api/parent/auth/bind",
            json={"code": "LEAKED12"},
        )
        assert bind_resp.status_code == 400, bind_resp.text
        # 訊息對齊 bind-additional 既有 idiom（綁定相關訊息）
        detail = bind_resp.json().get("detail", "")
        assert "綁定" in detail or "監護人" in detail

        # DB 守衛：Guardian.user_id 仍為原家長 A，未被覆寫
        with session_factory() as session:
            g = session.query(Guardian).filter(Guardian.id == guardian_id).first()
            assert (
                g.user_id == victim_parent_id
            ), "guardian.user_id 不應被覆寫；攻擊者持外洩碼成功竊取了綁定"

    def test_legit_first_bind_succeeds_when_guardian_user_id_is_null(
        self, parent_client
    ):
        """guardian.user_id IS NULL → 首次綁定走正常流程，回 200。"""
        client, session_factory = parent_client

        with session_factory() as session:
            admin = _create_admin_user(session)
            student = _create_student(session, "首綁學生")
            guardian = _create_guardian(session, student, "首綁家長")
            assert guardian.user_id is None  # 未綁定
            _seed_binding_code(
                session, guardian, plain_code="FIRST123", created_by=admin.id
            )
            session.commit()
            guardian_id = guardian.id

        # 新家長 LIFF 登入
        liff_resp = client.post(
            "/api/parent/auth/liff-login",
            json={"id_token": "token-fresh-parent"},
        )
        assert liff_resp.status_code == 200
        assert liff_resp.json()["status"] == "need_binding"

        # bind 應成功
        bind_resp = client.post(
            "/api/parent/auth/bind",
            json={"code": "FIRST123"},
        )
        assert bind_resp.status_code == 200, bind_resp.text
        body = bind_resp.json()
        assert body["status"] == "ok"
        assert body["user"]["role"] == "parent"

        # DB 上 Guardian.user_id 已綁到新建的 parent user
        with session_factory() as session:
            g = session.query(Guardian).filter(Guardian.id == guardian_id).first()
            assert g.user_id is not None
            bound_user = session.query(User).filter(User.id == g.user_id).first()
            assert bound_user is not None
            assert bound_user.role == "parent"
            assert bound_user.line_user_id == "U_fresh_parent_001"
