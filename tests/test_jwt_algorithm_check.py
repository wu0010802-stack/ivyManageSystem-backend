"""
JWT 算法混淆攻擊防護測試

驗證 decode_token 與 decode_token_allow_expired 在以下情境均拒絕（HTTPException 401）：
  - alg:none 攻擊（含大小寫變體）
  - 不在白名單的算法（RS256、HS512 等）
  - header 缺少 alg 欄位或為空字串

測試策略：手動組裝 JWT（不使用正常 library 簽名流程），確認防護不依賴簽名驗證才生效。
"""

import base64
import json
import time
import pytest
from unittest.mock import patch
from fastapi import HTTPException

from utils.auth import (
    create_access_token,
    decode_token,
    decode_token_allow_expired,
    JWT_ALGORITHM,
)


# ── 輔助函式：手動組裝 JWT ─────────────────────────────────────────────────

def _b64url_encode(data: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def _craft_jwt(header: dict, payload: dict, signature: str = "fakesig") -> str:
    """組裝一個 header 可任意指定的 JWT（signature 不具實際效力）。"""
    return f"{_b64url_encode(header)}.{_b64url_encode(payload)}.{signature}"


def _future_payload() -> dict:
    """一個尚未過期的 payload，確保測試不因過期而提前失敗。"""
    return {"user_id": 1, "role": "teacher", "exp": int(time.time()) + 300}


# ── decode_token 防護測試 ─────────────────────────────────────────────────

class TestDecodeTokenAlgorithmCheck:

    def test_valid_hs256_token_accepted(self):
        """正常 HS256 token 應通過驗證"""
        token = create_access_token({"user_id": 1})
        payload = decode_token(token)
        assert payload["user_id"] == 1

    def test_alg_none_lowercase_raises_401(self):
        """alg:none（小寫）攻擊 → 401"""
        token = _craft_jwt(
            {"alg": "none", "typ": "JWT"},
            _future_payload(),
            signature="",
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_alg_none_uppercase_raises_401(self):
        """alg:NONE（大寫）攻擊 → 401"""
        token = _craft_jwt(
            {"alg": "NONE", "typ": "JWT"},
            _future_payload(),
            signature="",
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_alg_none_with_fake_signature_raises_401(self):
        """alg:none 搭配偽造 signature → 401（不因有 signature 而跳過算法檢查）"""
        token = _craft_jwt(
            {"alg": "none", "typ": "JWT"},
            _future_payload(),
            signature="forged_signature_that_should_not_matter",
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_alg_rs256_raises_401(self):
        """header 聲稱 RS256（算法混淆攻擊）→ 401"""
        token = _craft_jwt(
            {"alg": "RS256", "typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_alg_hs512_raises_401(self):
        """header 聲稱 HS512（非白名單算法）→ 401"""
        token = _craft_jwt(
            {"alg": "HS512", "typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_alg_es256_raises_401(self):
        """header 聲稱 ES256（橢圓曲線，非白名單）→ 401"""
        token = _craft_jwt(
            {"alg": "ES256", "typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_missing_alg_field_raises_401(self):
        """header 完全缺少 alg 欄位 → 401"""
        token = _craft_jwt(
            {"typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_empty_alg_field_raises_401(self):
        """header 的 alg 為空字串 → 401"""
        token = _craft_jwt(
            {"alg": "", "typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_reject_happens_before_signature_check(self):
        """算法檢查必須在簽名驗證前發生：即使 payload 正確，alg 錯誤也應立即拒絕。
        此測試確認防護不依賴 library 的簽名驗證邏輯。"""
        # 用正確 secret 簽出的 HS256 token，但手動替換 header 為 alg:none
        legit = create_access_token({"user_id": 99})
        _header, body, sig = legit.split(".")
        forged_header = _b64url_encode({"alg": "none", "typ": "JWT"})
        tampered = f"{forged_header}.{body}.{sig}"
        with pytest.raises(HTTPException) as exc:
            decode_token(tampered)
        assert exc.value.status_code == 401


# ── 顯式 pre-check：即使 library 被 bypass 仍應攔截 ─────────────────────────

class TestAlgorithmCheckIsPreEmptive:
    """驗證算法檢查在 jwt.decode() 之前執行（defense-in-depth）。

    模擬情境：假設 python-jose 因升版 regression 而不再過濾 alg:none，
    我們自己的顯式 header 檢查仍應攔截攻擊。
    """

    def test_alg_none_blocked_even_if_library_bypassed(self):
        token = _craft_jwt(
            {"alg": "none", "typ": "JWT"},
            _future_payload(),
            signature="",
        )
        # 模擬 library regression：jwt.decode 直接回傳 payload，不驗算法
        with patch("utils.auth.jwt.decode", return_value=_future_payload()):
            with pytest.raises(HTTPException) as exc:
                decode_token(token)
            assert exc.value.status_code == 401

    def test_rs256_blocked_even_if_library_bypassed(self):
        token = _craft_jwt(
            {"alg": "RS256", "typ": "JWT"},
            _future_payload(),
        )
        with patch("utils.auth.jwt.decode", return_value=_future_payload()):
            with pytest.raises(HTTPException) as exc:
                decode_token(token)
            assert exc.value.status_code == 401

    def test_allow_expired_alg_none_blocked_even_if_library_bypassed(self):
        token = _craft_jwt(
            {"alg": "none", "typ": "JWT"},
            _future_payload(),
            signature="",
        )
        with patch("utils.auth.jwt.decode", return_value=_future_payload()):
            with pytest.raises(HTTPException) as exc:
                decode_token_allow_expired(token)
            assert exc.value.status_code == 401


# ── decode_token_allow_expired 同樣受保護 ─────────────────────────────────

class TestDecodeTokenAllowExpiredAlgorithmCheck:

    def test_valid_hs256_still_accepted(self):
        """refresh 路徑：正常 HS256 token 仍應通過"""
        token = create_access_token({"user_id": 1})
        payload = decode_token_allow_expired(token)
        assert payload["user_id"] == 1

    def test_alg_none_on_refresh_path_raises_401(self):
        """refresh 路徑的 alg:none 攻擊也必須被拒絕"""
        token = _craft_jwt(
            {"alg": "none", "typ": "JWT"},
            _future_payload(),
            signature="",
        )
        with pytest.raises(HTTPException) as exc:
            decode_token_allow_expired(token)
        assert exc.value.status_code == 401

    def test_alg_rs256_on_refresh_path_raises_401(self):
        """refresh 路徑的算法混淆攻擊也必須被拒絕"""
        token = _craft_jwt(
            {"alg": "RS256", "typ": "JWT"},
            _future_payload(),
        )
        with pytest.raises(HTTPException) as exc:
            decode_token_allow_expired(token)
        assert exc.value.status_code == 401
