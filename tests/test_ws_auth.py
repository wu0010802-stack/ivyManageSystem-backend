"""
verify_ws_token 單元測試

驗證 WS 認證函式在以下情境下的行為：
1. 有效 token + 帳號正常 → 回傳 payload
2. token 無效 / 過期 → HTTPException 401
3. 帳號已停用（is_active=False）→ HTTPException 401
4. token_version 不符 → HTTPException 401
5. must_change_password=True → HTTPException 403
6. payload 不含 user_id（無帳號綁定）→ 直接回傳 payload，不查 DB

注意：verify_ws_token 中的 get_session 是函式內延遲匯入，
因此 mock 路徑為 models.database.get_session。
"""

import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from utils.auth import create_access_token, verify_ws_token

# patch 路徑：函式內 `from models.database import get_session` 解析的實際來源
_PATCH_GET_SESSION = "models.database.get_session"


def _make_token(user_id: int = 1, token_version: int = 0, **extra) -> str:
    return create_access_token({
        "user_id": user_id,
        "token_version": token_version,
        "role": "teacher",
        **extra,
    })


def _mock_user(
    is_active: bool = True,
    token_version: int = 0,
    must_change_password: bool = False,
) -> MagicMock:
    u = MagicMock()
    u.is_active = is_active
    u.token_version = token_version
    u.must_change_password = must_change_password
    return u


# ── 正常情境 ───────────────────────────────────────────────────────────────

class TestVerifyWsTokenValid:
    def test_valid_token_active_user_returns_payload(self):
        token = _make_token(user_id=42, token_version=3)
        user = _mock_user(is_active=True, token_version=3)
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = user
            mock_get_session.return_value = session
            payload = verify_ws_token(token)
        assert payload["user_id"] == 42

    def test_no_user_id_skips_db_lookup(self):
        """payload 不含 user_id 時不查 DB，直接回傳 payload"""
        token = create_access_token({"employee_id": 99, "role": "teacher"})
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            payload = verify_ws_token(token)
            mock_get_session.assert_not_called()
        assert payload["employee_id"] == 99


# ── token 本身無效 ────────────────────────────────────────────────────────

class TestVerifyWsTokenInvalidJwt:
    def test_garbage_token_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            verify_ws_token("this.is.garbage")
        assert exc.value.status_code == 401

    def test_expired_token_raises_401(self):
        token = create_access_token(
            {"user_id": 1, "token_version": 0},
            expires_delta=timedelta(seconds=-1),
        )
        with pytest.raises(HTTPException) as exc:
            verify_ws_token(token)
        assert exc.value.status_code == 401


# ── 帳號狀態檢查 ──────────────────────────────────────────────────────────

class TestVerifyWsTokenAccountChecks:
    def test_inactive_user_raises_401(self):
        """帳號已停用 → 401"""
        token = _make_token(user_id=10, token_version=0)
        user = _mock_user(is_active=False, token_version=0)
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = user
            mock_get_session.return_value = session
            with pytest.raises(HTTPException) as exc:
                verify_ws_token(token)
        assert exc.value.status_code == 401

    def test_user_not_found_raises_401(self):
        """DB 查不到 user → 401"""
        token = _make_token(user_id=999, token_version=0)
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_get_session.return_value = session
            with pytest.raises(HTTPException) as exc:
                verify_ws_token(token)
        assert exc.value.status_code == 401

    def test_token_version_mismatch_raises_401(self):
        """token_version 不符（密碼已變更 / 強制登出）→ 401"""
        token = _make_token(user_id=5, token_version=2)
        user = _mock_user(is_active=True, token_version=3)  # DB 版本更新了
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = user
            mock_get_session.return_value = session
            with pytest.raises(HTTPException) as exc:
                verify_ws_token(token)
        assert exc.value.status_code == 401

    def test_must_change_password_raises_403(self):
        """首次登入尚未改密碼 → 403（非 401，讓 WS 端點可給出明確提示）"""
        token = _make_token(user_id=7, token_version=0)
        user = _mock_user(is_active=True, token_version=0, must_change_password=True)
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = user
            mock_get_session.return_value = session
            with pytest.raises(HTTPException) as exc:
                verify_ws_token(token)
        assert exc.value.status_code == 403

    def test_session_always_closed(self):
        """無論成功或失敗，DB session 都必須關閉"""
        token = _make_token(user_id=1, token_version=0)
        user = _mock_user(is_active=False)
        with patch(_PATCH_GET_SESSION) as mock_get_session:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = user
            mock_get_session.return_value = session
            with pytest.raises(HTTPException):
                verify_ws_token(token)
            session.close.assert_called_once()
