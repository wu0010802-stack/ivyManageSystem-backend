"""SEC-2026-0624-01 / -02 回歸：家長綁定 PII 落 log 截短 + bind-additional 限流。

SEC-01（PII 落 log）：parent_portal/auth.py 三處 logger.warning 原以完整 LINE
userId（`U`+多字元，可直接對映真實 LINE 帳號的個資）寫入應用日誌，違反
services/line_service.py 一律 line_user_id[:8] 的截短慣例。三處：
  - _check_bind_lockout（綁定失敗鎖定，line 244）
  - bind_first_child 拒絕覆寫綁定（line 651）
  - bind_first_child 綁定成功稽核（line 684）
修法：三處改 line_user_id[:8]，log 只留前 8 碼。

SEC-02（bind-additional 限流）：POST /parent/auth/bind-additional 是三個綁定碼
入口中唯一無失敗鎖定者。補 per-user（user_id）失敗鎖定，對齊 bind_first_child /
device_setup。限流亦使失敗訊息 oracle 無法被大量採集（細分訊息對合法家長有 UX
價值且有既有測試保護，故保留，僅加限流）。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
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
from models.database import Base, Guardian, GuardianBindingCode, Student, User
from utils.auth import create_access_token, hash_password
from utils.exception_handlers import register_exception_handlers

_SUCCESS_LINE_UID = "U_success_parent_full_0001"
_ATTACKER_LINE_UID = "U_attacker_parent_full_999"
_VICTIM_LINE_UID = "U_victim_parent_full_0001"


class FakeLineLoginService:
    def __init__(self, sub_map=None):
        self.sub_map = dict(sub_map or {})

    def is_configured(self):
        return True

    def verify_id_token(self, id_token: str) -> dict:
        if id_token in self.sub_map:
            return {
                "sub": self.sub_map[id_token],
                "aud": "test-channel",
                "name": "Fake",
            }
        raise HTTPException(status_code=401, detail="LINE id_token 驗證失敗")


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "bind-pii-log.sqlite"
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
    _bind_failures.clear()

    init_parent_line_service(
        FakeLineLoginService(
            {
                "token-success": _SUCCESS_LINE_UID,
                "token-attacker": _ATTACKER_LINE_UID,
            }
        )
    )

    app = FastAPI()
    register_exception_handlers(app)
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


# ── helpers ──────────────────────────────────────────────────────────────


def _create_admin(session) -> User:
    user = User(
        employee_id=None,
        username="bind_admin",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _create_parent_user(session, line_user_id: str) -> User:
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        must_change_password=False,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


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


def _seed_binding_code(session, guardian, *, plain_code: str, created_by: int):
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


@contextlib.contextmanager
def _without_global_pii_log_filter():
    """暫時移除 root handler 上的 PIIRedactionFilter。

    prod 由 main._configure_logging 掛全域 PIIRedactionFilter，會把任何
    `line_user_id=<value>` 整段遮成 `[Filtered]`（line_user_id 在 PII denylist）——
    那是更外層、主要的縱深防禦。但其行為依「是否曾 import main」而定（套件其他
    測試會觸發），會污染本地端 [:8] 截短的驗證。本 cm 在驗證視窗內剝除該 filter，
    使測試確定性地檢查「call-site 截短本身不洩完整值」，與套件順序無關。
    """
    from utils.log_pii_filter import PIIRedactionFilter

    removed = []
    for h in logging.root.handlers:
        for f in list(getattr(h, "filters", [])):
            if isinstance(f, PIIRedactionFilter):
                h.removeFilter(f)
                removed.append((h, f))
    try:
        yield
    finally:
        for h, f in removed:
            h.addFilter(f)


# ── SEC-01：三處 log 不得記錄完整 LINE userId ──────────────────────────────


def test_successful_first_bind_does_not_log_full_line_user_id(parent_client, caplog):
    """綁定成功稽核 log（site 684）只留 line_user_id[:8]，不得有完整值。"""
    client, session_factory = parent_client
    with session_factory() as session:
        admin = _create_admin(session)
        student = _create_student(session, "成功")
        guardian = _create_guardian(session, student, "成功家長")
        _seed_binding_code(
            session, guardian, plain_code="GOODCODE", created_by=admin.id
        )
        session.commit()

    liff = client.post(
        "/api/parent/auth/liff-login", json={"id_token": "token-success"}
    )
    assert liff.status_code == 200 and liff.json()["status"] == "need_binding"

    with _without_global_pii_log_filter(), caplog.at_level(logging.WARNING):
        resp = client.post("/api/parent/auth/bind", json={"code": "GOODCODE"})
    assert resp.status_code == 200, resp.text
    assert _SUCCESS_LINE_UID not in caplog.text  # call-site 截短：完整值不外洩
    assert _SUCCESS_LINE_UID[:8] in caplog.text  # 截短前綴仍在（稽核可辨識）


def test_takeover_rejection_does_not_log_full_line_user_id(parent_client, caplog):
    """拒絕覆寫綁定 log（site 651）只留攻擊者 line_user_id[:8]，不得有完整值。"""
    client, session_factory = parent_client
    with session_factory() as session:
        admin = _create_admin(session)
        victim = _create_parent_user(session, _VICTIM_LINE_UID)
        student = _create_student(session, "受害")
        guardian = _create_guardian(session, student, "受害家長")
        guardian.user_id = victim.id  # 已被合法家長綁定
        _seed_binding_code(
            session, guardian, plain_code="LEAKED12", created_by=admin.id
        )
        session.commit()

    liff = client.post(
        "/api/parent/auth/liff-login", json={"id_token": "token-attacker"}
    )
    assert liff.status_code == 200 and liff.json()["status"] == "need_binding"

    with _without_global_pii_log_filter(), caplog.at_level(logging.WARNING):
        resp = client.post("/api/parent/auth/bind", json={"code": "LEAKED12"})
    assert resp.status_code == 400, resp.text
    assert _ATTACKER_LINE_UID not in caplog.text
    assert _ATTACKER_LINE_UID[:8] in caplog.text


def test_bind_lockout_log_truncates_line_user_id(monkeypatch, caplog):
    """_check_bind_lockout 鎖定 log（site 244）只留 line_user_id[:8]。"""
    from api.parent_portal import auth as parent_auth

    monkeypatch.setattr("utils.rate_limit_db.count_recent_attempts", lambda *a, **k: 99)
    full = "U_lockout_victim_full_abcdef0001"
    with _without_global_pii_log_filter(), caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc:
            parent_auth._check_bind_lockout(full)
    assert exc.value.status_code == 429
    assert full not in caplog.text
    assert full[:8] in caplog.text


# ── SEC-02：bind-additional 失敗鎖定 ───────────────────────────────────────


def test_bind_additional_locks_out_after_repeated_failures(parent_client):
    """連續 5 次猜碼失敗後，第 6 次 bind-additional 回 429（per-user 鎖定）。"""
    client, session_factory = parent_client
    with session_factory() as session:
        parent_user = _create_parent_user(session, "U_bind_add_bruteforcer_01")
        session.commit()
        parent_user_id = parent_user.id

    token = create_access_token(
        {
            "user_id": parent_user_id,
            "employee_id": None,
            "role": "parent",
            "name": "parent_line_U_bind_add_bruteforcer_01",
            "permission_names": [],
            "token_version": 0,
        }
    )
    cookies = {"access_token": token}

    for i in range(5):
        resp = client.post(
            "/api/parent/auth/bind-additional",
            json={"code": f"BOGUS{i:03d}"},
            cookies=cookies,
        )
        assert resp.status_code == 400, f"第 {i} 次應為 400，得 {resp.status_code}"

    locked = client.post(
        "/api/parent/auth/bind-additional", json={"code": "BOGUS999"}, cookies=cookies
    )
    assert locked.status_code == 429, locked.text
