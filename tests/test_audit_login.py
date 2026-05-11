"""驗證 write_login_audit helper 與 _should_audit_block 對 login path 的例外。

A 階段（spec 2026-05-11-audit-coverage-gap）：登入事件補登 audit_logs。
"""

import ipaddress
import json
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
        "query_string": b"",
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

    def test_extras_username_does_not_override_authoritative_username(
        self, sqlite_engine
    ):
        """caller 傳 extras={'username': '...'} 不應蓋過明確 username 參數。
        Refs: Task 1 code review Issue 2。
        """
        request = _fake_request()
        write_login_audit(
            request,
            action="LOGIN_FAILED",
            username="alice",
            extras={"username": "MALICIOUS", "reason": "wrong_credentials"},
        )
        time.sleep(0.05)
        session = base_module._SessionFactory()
        try:
            rows = session.query(AuditLog).all()
        finally:
            session.close()
        assert len(rows) == 1
        changes = json.loads(rows[0].changes)
        assert changes["username"] == "alice"  # 權威參數獲勝
        # row.username 欄位也應該是 alice，不是 MALICIOUS
        assert rows[0].username == "alice"


class TestLoginEndpointAudit:
    """測試 /api/auth/login 五個分支與 WiFi 拒絕都會寫 audit_logs。"""

    @pytest.fixture
    def client_with_db(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.auth import router as auth_router
        from api.auth import _ip_attempts, _account_failures
        from models.database import User
        from utils.auth import hash_password

        db_path = tmp_path / "auth-audit.sqlite"
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
        # 清空 rate limit / lockout 計數
        try:
            _ip_attempts.clear()
            _account_failures.clear()
        except Exception:
            pass

        # 建一個測試用 admin
        session = session_factory()
        admin = User(
            username="alice",
            password_hash=hash_password("CorrectPass1"),
            role="admin",
            is_active=True,
        )
        session.add(admin)
        session.commit()
        session.close()

        app = FastAPI()
        app.include_router(auth_router)
        with TestClient(app) as client:
            yield client, session_factory

        base_module._engine = old_engine
        base_module._SessionFactory = old_factory
        engine.dispose()

    def _get_login_audits(self, session_factory):
        time.sleep(0.05)  # 等背景寫入
        session = session_factory()
        try:
            rows = (
                session.query(AuditLog)
                .filter(AuditLog.entity_type == "auth")
                .order_by(AuditLog.id)
                .all()
            )
        finally:
            session.close()
        return rows

    def test_login_success_creates_audit(self, client_with_db):
        client, sf = client_with_db
        res = client.post(
            "/api/auth/login", json={"username": "alice", "password": "CorrectPass1"}
        )
        assert res.status_code == 200, res.text
        rows = self._get_login_audits(sf)
        assert any(r.action == "LOGIN_SUCCESS" and r.username == "alice" for r in rows)

    def test_login_wrong_password_creates_failed_audit(self, client_with_db):
        client, sf = client_with_db
        res = client.post(
            "/api/auth/login", json={"username": "alice", "password": "WrongPass"}
        )
        assert res.status_code == 401
        rows = self._get_login_audits(sf)
        failed_rows = [r for r in rows if r.action == "LOGIN_FAILED"]
        assert failed_rows, "wrong password 必須記 LOGIN_FAILED"
        changes = json.loads(failed_rows[0].changes)
        assert changes["reason"] == "wrong_credentials"

    def test_login_unknown_username_creates_failed_audit_same_reason(
        self, client_with_db
    ):
        client, sf = client_with_db
        res = client.post(
            "/api/auth/login", json={"username": "ghost", "password": "AnyPass"}
        )
        assert res.status_code == 401
        rows = self._get_login_audits(sf)
        failed = [r for r in rows if r.action == "LOGIN_FAILED"]
        assert failed, "未知帳號也應記 LOGIN_FAILED"
        changes = json.loads(failed[0].changes)
        # 防帳號列舉：與密碼錯誤同 reason
        assert changes["reason"] == "wrong_credentials"
        # 失敗事件不寫 user_id（防 audit 洩漏帳號存在性）
        assert failed[0].entity_id is None

    def test_login_failed_does_not_record_password(self, client_with_db):
        client, sf = client_with_db
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "SecretLeakedPass"},
        )
        rows = self._get_login_audits(sf)
        for r in rows:
            haystack = f"{r.summary or ''} {r.changes or ''}"
            assert "SecretLeakedPass" not in haystack, "audit 不可包含明文密碼"
            # 也不可包含 password_hash 字串
            assert "password_hash" not in haystack

    def test_login_failed_bypasses_dedup(self, client_with_db):
        """連續多筆 LOGIN_FAILED 都會寫入，不被 path-based dedup 壓掉。"""
        client, sf = client_with_db
        for _ in range(3):
            client.post(
                "/api/auth/login", json={"username": "alice", "password": "WrongPass"}
            )
        rows = self._get_login_audits(sf)
        failed = [r for r in rows if r.action == "LOGIN_FAILED"]
        assert len(failed) >= 3, f"預期至少 3 筆 LOGIN_FAILED，實得 {len(failed)} 筆"

    def test_login_account_locked_creates_audit(self, client_with_db):
        """連續 _FAIL_THRESHOLD 次密碼錯誤觸發帳號鎖定，下一次嘗試應寫 LOGIN_LOCKED audit。"""
        client, sf = client_with_db
        from api.auth import _FAIL_THRESHOLD

        # 打到鎖定門檻：_check_account_lockout 在 _FAIL_THRESHOLD 筆失敗後才鎖定
        # 每次: check（count < threshold, pass）→ wrong password → record failure
        # 第 threshold+1 次：check 時 count == threshold >= threshold → LOCKED
        for _ in range(_FAIL_THRESHOLD):
            client.post(
                "/api/auth/login", json={"username": "alice", "password": "WrongPass"}
            )
        # 下一次觸發 lockout（即使密碼正確也會被帳號鎖定守衛擋住）
        res = client.post(
            "/api/auth/login", json={"username": "alice", "password": "CorrectPass1"}
        )
        assert (
            res.status_code == 429
        ), f"預期 429 lockout，實得 {res.status_code}: {res.text}"
        rows = self._get_login_audits(sf)
        locked = [r for r in rows if r.action == "LOGIN_LOCKED"]
        assert (
            locked
        ), f"未找到 LOGIN_LOCKED audit；現有 actions: {[r.action for r in rows]}"
        changes = json.loads(locked[0].changes)
        assert changes["scope"] == "account_lockout"

    def test_login_ip_rate_limited_creates_audit(self, client_with_db):
        """同 IP 在短時間內過多嘗試應觸發 IP 限流，寫 LOGIN_RATE_LIMITED audit。"""
        client, sf = client_with_db
        from api.auth import _IP_MAX_ATTEMPTS

        # record_attempt 在 count 前執行，故第 _IP_MAX_ATTEMPTS+1 次 count > 20 → 觸發
        # 用不同 username 避免帳號鎖定干擾
        for i in range(_IP_MAX_ATTEMPTS + 1):
            client.post(
                "/api/auth/login",
                json={"username": f"nonexistent_user_{i}", "password": "AnyPass"},
            )
        rows = self._get_login_audits(sf)
        rate_limited = [r for r in rows if r.action == "LOGIN_RATE_LIMITED"]
        assert rate_limited, (
            f"未找到 LOGIN_RATE_LIMITED audit；現有 actions: "
            f"{[r.action for r in rows]} (total {len(rows)})"
        )
        changes = json.loads(rate_limited[0].changes)
        assert changes["scope"] == "ip_sliding_window"

    def test_login_teacher_non_school_wifi_creates_audit(
        self, client_with_db, monkeypatch
    ):
        """教師角色從非校園 WiFi 登入應寫 LOGIN_FAILED + reason='non_school_wifi'。"""
        client, sf = client_with_db
        from models.database import User
        from utils.auth import hash_password

        # 建一名 teacher
        session = sf()
        try:
            teacher = User(
                username="bob_teacher",
                password_hash=hash_password("TeacherPass1"),
                role="teacher",
                is_active=True,
            )
            session.add(teacher)
            session.commit()
        finally:
            session.close()

        # monkeypatch _get_school_wifi_networks 回傳包含真實 ip_network 物件的清單
        # TestClient 的 request.client.host 為 "testclient"，ipaddress.ip_address("testclient")
        # 會拋 ValueError，故 _is_school_wifi 回 False（從非校園 IP 視角拒絕）
        import api.auth as auth_module

        monkeypatch.setattr(
            auth_module,
            "_get_school_wifi_networks",
            lambda: [ipaddress.ip_network("192.168.99.0/24")],
        )

        res = client.post(
            "/api/auth/login",
            json={"username": "bob_teacher", "password": "TeacherPass1"},
        )
        assert (
            res.status_code == 403
        ), f"預期 403 WiFi 拒絕，實得 {res.status_code}: {res.text}"

        rows = self._get_login_audits(sf)
        wifi_failures = [
            r
            for r in rows
            if r.action == "LOGIN_FAILED"
            and json.loads(r.changes or "{}").get("reason") == "non_school_wifi"
        ]
        assert wifi_failures, (
            f"未找到 non_school_wifi 失敗 audit；現有 actions/reasons: "
            f"{[(r.action, json.loads(r.changes or '{}').get('reason')) for r in rows]}"
        )
        assert wifi_failures[0].username == "bob_teacher"

    def test_logout_creates_audit(self, client_with_db):
        client, sf = client_with_db
        # 先登入取得 cookie
        res_login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "CorrectPass1"},
        )
        assert res_login.status_code == 200
        # 再登出
        res = client.post("/api/auth/logout")
        assert res.status_code == 200, res.text
        rows = self._get_login_audits(sf)
        logouts = [r for r in rows if r.action == "LOGOUT"]
        assert logouts, f"未找到 LOGOUT audit；現有 actions: {[r.action for r in rows]}"

    def test_refresh_token_success_audit(self, client_with_db):
        client, sf = client_with_db
        # 先登入
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "CorrectPass1"},
        )
        # 用同一個 TestClient（會帶 cookie）打 refresh
        res = client.post("/api/auth/refresh")
        assert res.status_code == 200, res.text
        rows = self._get_login_audits(sf)
        refresh_rows = [r for r in rows if r.action == "TOKEN_REFRESH"]
        assert (
            refresh_rows
        ), f"未找到 TOKEN_REFRESH audit；現有 actions: {[r.action for r in rows]}"
        assert refresh_rows[0].username == "alice"

    def test_refresh_token_no_token_audits_failure(self, client_with_db):
        client, sf = client_with_db
        # 未登入直接 refresh
        res = client.post("/api/auth/refresh")
        assert res.status_code == 401
        rows = self._get_login_audits(sf)
        failed = [r for r in rows if r.action == "TOKEN_REFRESH_FAILED"]
        assert (
            failed
        ), f"未找到 TOKEN_REFRESH_FAILED audit；現有 actions: {[r.action for r in rows]}"
        changes = json.loads(failed[0].changes or "{}")
        assert changes.get("reason") == "no_token"
