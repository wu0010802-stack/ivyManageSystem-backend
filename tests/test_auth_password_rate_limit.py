"""Spec A PR-A2: change-password / reset-password 限流 6 個 pytest。

走實際 DB-backed counter（test fixture SQLite），鏡像 prod 行為；
不 mock utils.rate_limit_db 以維持 witness 強度。
"""

import os
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import (
    _ACCOUNT_SCOPE,
    _FAIL_LOCKOUT,
    _FAIL_THRESHOLD,
    _IP_MAX_ATTEMPTS,
    _check_account_lockout,
    router as auth_router,
)
from models.database import Base, User
from utils.auth import hash_password
from utils.permissions import Permission


def _make_app():
    app = FastAPI()
    app.include_router(auth_router)
    return app


def _create_user(
    session,
    *,
    username: str,
    password: str,
    role: str = "teacher",
    permission_names=None,
    must_change_password: bool = False,
) -> User:
    if permission_names is None:
        permission_names = []
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=permission_names,
        must_change_password=must_change_password,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def app_with_db(tmp_path):
    """隔離 SQLite DB + FastAPI app + TestClient。

    yield (client, session_factory, User物件字典) 元組。
    """
    db_path = tmp_path / "pwd_rate_limit.sqlite"
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

    app = _make_app()
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_as(client: TestClient, username: str, password: str) -> str:
    """Helper: 登入並回傳 access_token cookie value。"""
    res = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200, f"login 應成功, got {res.status_code}: {res.text}"
    cookie = res.cookies.get("access_token")
    assert cookie, "Set-Cookie 應含 access_token"
    return cookie


# ============ Test 1: change-password user lockout ============


def test_change_password_user_lockout(app_with_db):
    """同 user 連續 5 次 old_password 錯誤 → 第 6 次返回 429，audit 記 PASSWORD_CHANGE_LOCKED。"""
    client, session_factory = app_with_db
    with session_factory() as session:
        _create_user(session, username="t_pwd1", password="GoodOld123!")
        session.commit()

    token = _login_as(client, "t_pwd1", "GoodOld123!")
    client.cookies.set("access_token", token)

    # 連續 5 次故意打錯 old_password → 每次回 400，但累積 failure
    for i in range(5):
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert (
            res.status_code == 400
        ), f"第 {i+1} 次應 400 舊密碼錯誤, got {res.status_code}"

    # 第 6 次應觸發 lockout 429
    with patch("api.auth.write_login_audit") as audit_spy:
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert res.status_code == 429, f"第 6 次應 429 lockout, got {res.status_code}"
        assert "密碼修改失敗次數過多" in res.json()["detail"]
        # audit 記錄 PASSWORD_CHANGE_LOCKED
        actions = [c.kwargs.get("action") for c in audit_spy.call_args_list]
        assert "PASSWORD_CHANGE_LOCKED" in actions, f"audit actions: {actions}"


# ============ Test 2: change-password clear failures on success ============


def test_change_password_clear_failures_on_success(app_with_db):
    """失敗 4 次 + 成功 1 次 → 再失敗 1 次不應 lockout（counter 已 clear）。"""
    client, session_factory = app_with_db
    with session_factory() as session:
        _create_user(session, username="t_pwd2", password="GoodOld123!")
        session.commit()

    token = _login_as(client, "t_pwd2", "GoodOld123!")
    client.cookies.set("access_token", token)

    # 失敗 4 次
    for _ in range(4):
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert res.status_code == 400

    # 成功一次（用正確 old_password）
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "GoodOld123!", "new_password": "NewGood456!"},
    )
    assert res.status_code == 200, res.text

    # 用新密碼重 login（前次 change-password 已 invalidate token_version）
    new_token = _login_as(client, "t_pwd2", "NewGood456!")
    client.cookies.set("access_token", new_token)

    # 再失敗 1 次 → 應仍 400（不是 429），counter 已 clear
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "WrongAgain", "new_password": "Another789!"},
    )
    assert res.status_code == 400, f"counter 應已 clear, got {res.status_code}"


# ============ Test 3: change-password IP-only rate limit ============


def test_change_password_ip_only_rate_limit(app_with_db):
    """單獨驗證 IP 層。同 IP 透過多個不同 user 嘗試累積 > 20 次 → 觸發 IP 429，
    audit 記 PASSWORD_CHANGE_RATE_LIMITED。

    用「正確 old_password 但 new_password 故意觸發 validate_password_strength fail」
    的方式累積 IP 計數而不觸發 user lockout（verify_password 成功，不呼叫
    _record_pwd_change_failure）。

    注意：token 直接用 create_access_token 產生，不走 login endpoint，
    避免 login IP 限流（20 次）提早阻斷後續 login 請求干擾本測試。
    """
    from utils.auth import create_access_token

    client, session_factory = app_with_db

    # 預建 22 user（每次用不同 user 避免單一 user lockout 提前截斷計數）
    users = []
    with session_factory() as session:
        for i in range(22):
            u = _create_user(
                session, username=f"t_ipuser{i}", password="Good123!", role="teacher"
            )
            users.append((f"t_ipuser{i}", u.id))
        session.commit()

    triggered = False
    audit_spy_calls = []
    for i, (uname, uid) in enumerate(users):
        # 直接產 JWT，不走 login endpoint（避免 login IP 限流干擾）
        token = create_access_token(
            {
                "user_id": uid,
                "role": "teacher",
                "permission_names": [],
                "token_version": 0,
            }
        )
        client.cookies.set("access_token", token)

        with patch("api.auth.write_login_audit") as audit_spy:
            # 正確舊密碼 + 太短的新密碼 → validate_password_strength fail → 400
            # IP 計數已在 _check_pwd_change_ip 前端記錄（每次 record_attempt 都計）
            res = client.post(
                "/api/auth/change-password",
                json={"old_password": "Good123!", "new_password": "short"},
            )
            calls_this_round = [
                c.kwargs.get("action") for c in audit_spy.call_args_list
            ]
            audit_spy_calls.extend(calls_this_round)
            if res.status_code == 429:
                triggered = True
                assert "請求過於頻繁" in res.json()["detail"]
                break

        assert res.status_code in (
            400,
            429,
        ), f"i={i} unexpected {res.status_code}: {res.text}"

    assert triggered, f"連續 {len(users)} 次未觸發 IP 429，IP 計數可能未生效"
    assert (
        "PASSWORD_CHANGE_RATE_LIMITED" in audit_spy_calls
    ), f"audit 應有 PASSWORD_CHANGE_RATE_LIMITED, got: {audit_spy_calls}"


# ============ Test 4: reset-password IP rate limit ============


def test_reset_password_ip_rate_limit(app_with_db):
    """admin 同 IP 連續 20 次 reset → 第 21 次回 429，
    audit 記 PASSWORD_RESET_RATE_LIMITED + extras.target_user_id。"""
    client, session_factory = app_with_db
    with session_factory() as session:
        admin = _create_user(
            session,
            username="t_admin1",
            password="AdminGood1!",
            role="admin",
            permission_names=["USER_MANAGEMENT_WRITE", "*"],
        )
        target = _create_user(
            session, username="t_resetv1", password="Target123!", role="teacher"
        )
        target_id = target.id
        session.commit()

    token = _login_as(client, "t_admin1", "AdminGood1!")
    client.cookies.set("access_token", token)

    # 連續 20 次 reset（每次都成功 200）
    for i in range(20):
        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": f"NewPw{i}!AbcDef"},
        )
        assert res.status_code == 200, f"第 {i+1} 次應成功 200, got {res.status_code}"

    # 第 21 次應 429
    with patch("api.auth.write_login_audit") as audit_spy:
        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": "Final123!"},
        )
        assert res.status_code == 429, f"第 21 次應 429, got {res.status_code}"
        actions = [c.kwargs.get("action") for c in audit_spy.call_args_list]
        assert "PASSWORD_RESET_RATE_LIMITED" in actions

        # 確認 extras 含 target_user_id
        matching_calls = [
            c
            for c in audit_spy.call_args_list
            if c.kwargs.get("action") == "PASSWORD_RESET_RATE_LIMITED"
        ]
        assert matching_calls
        extras = matching_calls[0].kwargs.get("extras", {})
        assert extras.get("target_user_id") == target_id


# ============ Test 5: reset-password no target user lockout ============


def test_reset_password_no_target_user_lockout(app_with_db):
    """admin 對同一 target user 連續重設 10 次後，target user 沒被誤記入 login_account scope。"""
    client, session_factory = app_with_db
    with session_factory() as session:
        _create_user(
            session,
            username="t_admin2",
            password="AdminGood1!",
            role="admin",
            permission_names=["USER_MANAGEMENT_WRITE", "*"],
        )
        target = _create_user(
            session, username="t_target2", password="Target123!", role="teacher"
        )
        target_id = target.id
        target_username = target.username
        session.commit()

    token = _login_as(client, "t_admin2", "AdminGood1!")
    client.cookies.set("access_token", token)

    for i in range(10):
        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": f"NewPw{i}!AbcDef"},
        )
        assert res.status_code == 200, f"i={i}: {res.text}"

    # Assert 1: target.username 不應在 login_account scope 累積
    from utils.rate_limit_db import count_recent_attempts

    count = count_recent_attempts(
        _ACCOUNT_SCOPE, target_username, within_seconds=_FAIL_LOCKOUT
    )
    assert (
        count == 0
    ), f"target user 不應被誤記入 login_account scope, got count={count}"

    # Assert 2: _check_account_lockout(target.username) 不拋 429
    try:
        _check_account_lockout(target_username)
    except Exception as e:
        pytest.fail(f"target user 應可正常 login, 但 _check_account_lockout 拋 {e}")


# ============ Test 6: pwd_change scope isolated from login ============


def test_pwd_change_scope_isolated_from_login(app_with_db):
    """驗證 scope 隔離：
    - login 失敗 5 次（觸發 login_account lockout）→ pwd_change_user scope 計數仍為 0
    - change-password scope 不影響 login_account scope（反向隔離）
    """
    client, session_factory = app_with_db
    with session_factory() as session:
        user = _create_user(
            session, username="t_scope1", password="GoodOld123!", role="teacher"
        )
        user_id = user.id
        session.commit()

    # Part A: login 5 次失敗 → 觸發 login lockout
    for _ in range(5):
        client.post(
            "/api/auth/login",
            json={"username": "t_scope1", "password": "WrongLogin"},
        )
    # 第 6 次 login 應被 lockout
    res = client.post(
        "/api/auth/login",
        json={"username": "t_scope1", "password": "WrongLogin"},
    )
    assert res.status_code == 429, "login 應已 lockout"

    # login 失敗不應計入 pwd_change_user scope
    from api.auth import _PWD_CHANGE_USER_SCOPE
    from utils.rate_limit_db import clear_attempts, count_recent_attempts

    pwd_change_count = count_recent_attempts(
        _PWD_CHANGE_USER_SCOPE, f"user:{user_id}", within_seconds=_FAIL_LOCKOUT
    )
    assert (
        pwd_change_count == 0
    ), f"login 失敗不應計入 pwd_change_user scope, got {pwd_change_count}"

    # Part B: 真實反向隔離 — pwd_change_user 失敗計數不該洩漏進 login_account scope
    # 清掉 Part A 累積的 login lockout 讓 baseline 乾淨
    clear_attempts(_ACCOUNT_SCOPE, "t_scope1")
    baseline_login_count = count_recent_attempts(
        _ACCOUNT_SCOPE, "t_scope1", within_seconds=_FAIL_LOCKOUT
    )
    assert baseline_login_count == 0, f"清完應為 0, got {baseline_login_count}"

    # 直接灌 _FAIL_THRESHOLD 次 pwd_change failures（模擬 change-password 連敗）
    from api.auth import _record_pwd_change_failure

    for _ in range(_FAIL_THRESHOLD):
        _record_pwd_change_failure(user_id)

    # 驗 pwd_change_user scope 已累積到 threshold（自我確認 record 有效）
    pwd_change_after = count_recent_attempts(
        _PWD_CHANGE_USER_SCOPE, f"user:{user_id}", within_seconds=_FAIL_LOCKOUT
    )
    assert (
        pwd_change_after >= _FAIL_THRESHOLD
    ), f"_record_pwd_change_failure 應已累積到 {_FAIL_THRESHOLD}, got {pwd_change_after}"

    # 反向隔離驗收 1：login_account scope 不該因 pwd_change failure 累積
    login_count_after_pwd_fail = count_recent_attempts(
        _ACCOUNT_SCOPE, "t_scope1", within_seconds=_FAIL_LOCKOUT
    )
    assert (
        login_count_after_pwd_fail == 0
    ), f"pwd_change 失敗不應計入 login_account scope, got {login_count_after_pwd_fail}"

    # 反向隔離驗收 2：_check_account_lockout 不該因 pwd_change failure 拋 429
    try:
        _check_account_lockout("t_scope1")
    except Exception as e:
        pytest.fail(
            f"pwd_change 失敗不應觸發 login lockout, 但 _check_account_lockout 拋 {e}"
        )
