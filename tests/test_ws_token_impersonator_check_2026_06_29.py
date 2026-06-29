"""verify_ws_token 須比照 HTTP 路徑檢查 impersonator 狀態（is_active + token_version）。

QA loop（2026-06-29）：HTTP 路徑 _resolve_user_auth_fields 對模擬 token 會驗 impersonator
(admin) 的 is_active（#5 2026-06-17）與 token_version（qa-loop #4 2026-06-23），但 WS 路徑
verify_ws_token 只查被模擬者（target），完全忽略 impersonated_by。後果：admin 模擬 teacher
拿 token 開 dismissal / contact_book WS 後，即使 admin 被停用 / reset_password / logout-all，
WS 端看不出來，殘餘 token 效期內仍持續接收學生 PII。

修法：verify_ws_token 在 payload 帶 impersonated_by 時，於同一 session 查 impersonator 並驗
is_active + token_version，不符即 401（對齊 _resolve_user_auth_fields）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.database import User
from utils.auth import create_access_token, hash_password, verify_ws_token


def _mk_user(session, username, role, token_version=0, is_active=True):
    u = User(
        username=username,
        password_hash=hash_password("p"),
        role=role,
        is_active=is_active,
        token_version=token_version,
        must_change_password=False,
    )
    session.add(u)
    session.commit()
    return u


def _impersonation_token(target, admin, impersonator_token_version):
    return create_access_token(
        {
            "user_id": target.id,
            "token_version": target.token_version or 0,
            "role": "teacher",
            "impersonated_by": admin.id,
            "impersonator_token_version": impersonator_token_version,
        }
    )


def test_ws_blocks_when_impersonator_token_version_bumped(test_db_session):
    """admin token_version 在模擬飛行中被 bump（reset_password/改權/logout-all）→ 401。"""
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", token_version=5)  # DB 已 bump 到 5
    target = _mk_user(session, "tgt", "teacher")
    token = _impersonation_token(
        target, admin, impersonator_token_version=4
    )  # 簽發於 bump 前
    with pytest.raises(HTTPException) as exc:
        verify_ws_token(token)
    assert exc.value.status_code == 401


def test_ws_blocks_when_impersonator_inactive(test_db_session):
    """admin 帳號被停用 → 模擬 WS token 立即失效 401。"""
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", token_version=2, is_active=False)
    target = _mk_user(session, "tgt", "teacher")
    token = _impersonation_token(target, admin, impersonator_token_version=2)
    with pytest.raises(HTTPException) as exc:
        verify_ws_token(token)
    assert exc.value.status_code == 401


def test_ws_allows_when_impersonator_matches(test_db_session):
    """admin is_active 且 token_version 與 claim 一致 → 模擬 WS 照常通過。"""
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", token_version=3)
    target = _mk_user(session, "tgt", "teacher")
    token = _impersonation_token(target, admin, impersonator_token_version=3)
    payload = verify_ws_token(token)
    assert payload["user_id"] == target.id
    assert payload["impersonated_by"] == admin.id


def test_ws_non_impersonation_unaffected(test_db_session):
    """一般（非模擬）WS token 行為不變，不因新檢查受影響。"""
    session = test_db_session
    target = _mk_user(session, "tgt", "teacher")
    token = create_access_token(
        {"user_id": target.id, "token_version": 0, "role": "teacher"}
    )
    payload = verify_ws_token(token)
    assert payload["user_id"] == target.id
