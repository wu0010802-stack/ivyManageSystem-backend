"""qa-loop 廣掃（2026-07-01）P1：change_password 缺 impersonation 守衛。

/auth/change-password 全程用 current_user['user_id']（模擬期間為被冒充的 target，
非操作者）查/寫 User row，且未呼叫 _reject_if_impersonating()（該守衛已加在
/sessions 等三端點，見 test_auth_sessions_impersonation_guard_2026_06_29.py）。
操作者只需持有 PORTAL_IMPERSONATE（write 模式）並得知 target 目前密碼，即可真正
改掉 target 的 password_hash、清除 must_change_password、bump token_version、
撤銷 target 所有 refresh token family，且 target 完全不知情。修法：比照 /sessions
端點，模擬期間一律 403，禁止觸碰。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from models.database import User
from utils.auth import hash_password
import api.auth as auth_api


def _make_request(client_host="1.2.3.4", ua="pytest-ua"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/change-password",
        "headers": [(b"user-agent", ua.encode())],
        "client": (client_host, 12345),
        "query_string": b"",
    }
    return Request(scope)


def test_change_password_blocked_during_impersonation(test_db_session):
    """模擬期間呼叫 change_password 必須 403，且 target 密碼/狀態完全不受影響。"""
    from unittest.mock import patch

    session = test_db_session
    target = User(
        username="innocent_target",
        password_hash=hash_password("KnownDefault123!"),
        role="teacher",
        is_active=True,
        token_version=0,
        must_change_password=True,
    )
    session.add(target)
    session.commit()
    original_hash = target.password_hash
    original_token_version = target.token_version

    data = auth_api.ChangePasswordRequest(
        old_password="KnownDefault123!", new_password="AttackerChosenPass99"
    )
    # 隔離掉與本次守衛無關的依賴（HIBP 外呼、resolve_user_permissions 查 roles 表），
    # 讓「未加守衛時是否真的會改掉 target 密碼」成為唯一判準，不受其他相依錯誤污染。
    with (
        patch("utils.hibp.requests.get") as gh,
        patch.object(auth_api, "resolve_user_permissions", return_value=[]),
    ):
        gh.return_value.text = ""
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        with pytest.raises(HTTPException) as exc:
            auth_api.change_password(
                data,
                _make_request(),
                current_user={
                    "user_id": target.id,
                    "impersonated_by": 1,
                    "username": "innocent_target",
                },
            )
    assert exc.value.status_code == 403

    session.refresh(target)
    assert target.password_hash == original_hash, "模擬期間不可真的改掉 target 密碼"
    assert target.must_change_password is True
    assert target.token_version == original_token_version


def test_change_password_not_blocked_when_not_impersonating(test_db_session):
    """非模擬（無 impersonated_by）行為不變，本人改自己密碼照常成功。"""
    session = test_db_session
    user = User(
        username="normal_user",
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
    from unittest.mock import patch

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
            current_user={"user_id": user.id, "username": "normal_user"},
        )
    assert getattr(resp, "status_code", 200) != 403
