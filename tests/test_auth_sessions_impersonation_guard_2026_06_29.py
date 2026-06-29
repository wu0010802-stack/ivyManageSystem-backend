"""/sessions 端點與 change_password 的兩個 qa-loop round2（2026-06-29）修正。

P2：list_my_sessions / revoke_session / logout_all_sessions 用 current_user['user_id']，
模擬期間 access_token 的 user_id = 被模擬 target → 操作（含唯讀 PORTAL_PREVIEW 預覽）會作用到
無辜 target：洩漏其裝置 IP/UA、或強制登出其所有裝置 + bump 其 token_version。/sessions 端點
缺 impersonation 守衛。修法：模擬期間一律 403。

P3：change_password 撤掉「當前裝置」的 staff_refresh family 卻只重發 access token，未重發
refresh → 當前裝置在 access token（15min）到期後 /refresh 撞已撤 family 被踢，與註解
「避免改完密碼立刻被踢」矛盾。修法：比照 login 為當前裝置重新簽發 refresh family。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from models.database import User
from utils.auth import hash_password
import api.auth as auth_api


def _imp_user(target_id=5, admin_id=1):
    """模擬中的 current_user：user_id=target、impersonated_by=admin。"""
    return {"user_id": target_id, "impersonated_by": admin_id, "role": "teacher"}


def _make_request(client_host="1.2.3.4", ua="pytest-ua"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/x",
        "headers": [(b"user-agent", ua.encode())],
        "client": (client_host, 12345),
        "query_string": b"",
    }
    return Request(scope)


# ── P2：模擬期間 /sessions 端點一律 403 ───────────────────────────────────────


def test_logout_all_blocked_during_impersonation(test_db_session):
    with pytest.raises(HTTPException) as exc:
        auth_api.logout_all_sessions(request=MagicMock(), current_user=_imp_user())
    assert exc.value.status_code == 403


def test_revoke_session_blocked_during_impersonation(test_db_session):
    with pytest.raises(HTTPException) as exc:
        auth_api.revoke_session("fam-x", current_user=_imp_user())
    assert exc.value.status_code == 403


def test_list_my_sessions_blocked_during_impersonation(test_db_session):
    with pytest.raises(HTTPException) as exc:
        auth_api.list_my_sessions(request=MagicMock(), current_user=_imp_user())
    assert exc.value.status_code == 403


def test_logout_all_not_blocked_when_not_impersonating(test_db_session):
    """非模擬（impersonated_by 缺）→ 不應 403，照常回 JSONResponse。"""
    resp = auth_api.logout_all_sessions(
        request=MagicMock(), current_user={"user_id": 99999, "role": "admin"}
    )
    assert getattr(resp, "status_code", 200) != 403


# ── P3：change_password 重發 refresh cookie ──────────────────────────────────


def test_change_password_reissues_refresh_cookie(test_db_session):
    session = test_db_session
    user = User(
        username="pwduser",
        password_hash=hash_password("OldPassw0rd!!"),
        role="hr",
        is_active=True,
        token_version=0,
        must_change_password=False,
    )
    session.add(user)
    session.commit()

    data = auth_api.ChangePasswordRequest(
        old_password="OldPassw0rd!!", new_password="BrandNewPass99"
    )
    with (
        patch("utils.hibp.requests.get") as gh,
        patch.object(auth_api, "resolve_user_permissions", return_value=[]),
    ):
        gh.return_value.text = ""
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        resp = auth_api.change_password(
            data,
            _make_request(),
            current_user={"user_id": user.id, "username": "pwduser"},
        )

    set_cookies = b"\n".join(
        v for (k, v) in resp.raw_headers if k == b"set-cookie"
    ).decode()
    assert (
        "staff_refresh_token=" in set_cookies
    ), "改密碼後須為當前裝置重發 staff_refresh cookie，否則 ~15min 後被踢"
