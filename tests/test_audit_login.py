"""驗證 write_login_audit helper 與 _should_audit_block 對 login path 的例外。

A 階段（spec 2026-05-11-audit-coverage-gap）：登入事件補登 audit_logs。
"""

import os
import sys
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import AuditLog, Base
from utils.audit import (
    ACTION_LABELS,
    ENTITY_LABELS,
    _should_audit_block,
    write_login_audit,
)


@pytest.fixture
def sqlite_engine(tmp_path):
    db_path = tmp_path / "audit-login.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    yield engine

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def _fake_request(ip="10.0.0.1"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [],
        "client": (ip, 12345),
    }
    return Request(scope)


class TestActionAndEntityLabels:
    def test_login_actions_have_chinese_labels(self):
        for action in [
            "LOGIN_SUCCESS",
            "LOGIN_FAILED",
            "LOGIN_RATE_LIMITED",
            "LOGIN_LOCKED",
            "LOGOUT",
            "TOKEN_REFRESH",
            "TOKEN_REFRESH_FAILED",
        ]:
            assert action in ACTION_LABELS, f"{action} 必須有中文 label"

    def test_read_action_has_chinese_label(self):
        assert ACTION_LABELS["READ"] == "查看"

    def test_auth_entity_type_has_chinese_label(self):
        assert ENTITY_LABELS["auth"] == "登入活動"


class TestShouldAuditBlockLoginException:
    def test_login_path_bypasses_dedup(self):
        # 連續兩次同 ip+path 應該都回 True（不被 dedup 壓掉）
        assert _should_audit_block("1.2.3.4", "POST", "/api/auth/login") is True
        assert _should_audit_block("1.2.3.4", "POST", "/api/auth/login") is True

    def test_non_login_path_still_dedups(self):
        # 既有 dedup 行為對其他路徑不變
        ip = "5.6.7.8"
        path = "/api/employees/9"
        assert _should_audit_block(ip, "DELETE", path) is True
        assert _should_audit_block(ip, "DELETE", path) is False


class TestWriteLoginAuditHelper:
    def test_writes_audit_row_for_login_success(self, sqlite_engine):
        request = _fake_request(ip="10.0.0.1")
        write_login_audit(
            request,
            action="LOGIN_SUCCESS",
            username="alice",
            user_id=42,
            extras={"role": "admin"},
        )
        # 等待背景 task；fallback path 是同步寫入
        time.sleep(0.05)

        session_factory = base_module._SessionFactory
        session = session_factory()
        rows = session.query(AuditLog).all()
        session.close()

        assert len(rows) == 1
        row = rows[0]
        assert row.action == "LOGIN_SUCCESS"
        assert row.entity_type == "auth"
        assert row.entity_id == "42"
        assert row.username == "alice"
        # changes 是 JSON text
        import json

        changes = json.loads(row.changes)
        assert changes["username"] == "alice"
        assert changes["role"] == "admin"
        # 不能含密碼相關 key
        assert "password" not in changes
        assert "password_hash" not in changes

    def test_writes_audit_row_for_login_failed_without_user_id(self, sqlite_engine):
        request = _fake_request()
        write_login_audit(
            request,
            action="LOGIN_FAILED",
            username="bob",
            extras={"reason": "wrong_credentials"},
        )
        time.sleep(0.05)

        session_factory = base_module._SessionFactory
        session = session_factory()
        rows = session.query(AuditLog).all()
        session.close()

        assert len(rows) == 1
        assert rows[0].action == "LOGIN_FAILED"
        assert rows[0].entity_id is None  # 防帳號列舉：失敗不寫 user_id
