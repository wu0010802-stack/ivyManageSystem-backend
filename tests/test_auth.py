"""認證工具單元測試"""
import pytest
from datetime import timedelta
from fastapi import HTTPException
from utils.auth import (
    hash_password, verify_password,
    create_access_token, decode_token,
    JWT_SECRET_KEY, JWT_ALGORITHM
)


class TestHashPassword:

    def test_format(self):
        """雜湊結果包含 salt$hash 格式"""
        hashed = hash_password("test123")
        assert "$" in hashed
        salt, h = hashed.split("$", 1)
        assert len(salt) == 32  # 16 bytes hex
        assert len(h) > 0

    def test_different_salts(self):
        """相同密碼每次產生不同雜湊"""
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        assert h1 != h2


class TestVerifyPassword:

    def test_correct_password(self):
        """正確密碼驗證通過"""
        hashed = hash_password("correct")
        assert verify_password("correct", hashed) is True

    def test_wrong_password(self):
        """錯誤密碼驗證失敗"""
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_malformed_hash(self):
        """格式錯誤的雜湊不會 crash"""
        assert verify_password("test", "no_dollar_sign") is False

    def test_empty_hash(self):
        """空雜湊不會 crash"""
        assert verify_password("test", "") is False


class TestCreateAccessToken:

    def test_creates_token(self):
        """建立 token 成功"""
        token = create_access_token({"employee_id": "E001", "role": "teacher"})
        assert isinstance(token, str)
        assert len(token) > 0

    def test_payload_preserved(self):
        """payload 資料保留"""
        data = {"employee_id": "E001", "role": "admin"}
        token = create_access_token(data)
        payload = decode_token(token)
        assert payload["employee_id"] == "E001"
        assert payload["role"] == "admin"

    def test_has_expiration(self):
        """token 包含過期時間"""
        token = create_access_token({"id": "1"})
        payload = decode_token(token)
        assert "exp" in payload

    def test_custom_expiration(self):
        """自訂過期時間"""
        token = create_access_token({"id": "1"}, expires_delta=timedelta(minutes=5))
        payload = decode_token(token)
        assert "exp" in payload


class TestDecodeToken:

    def test_valid_token(self):
        """有效 token 解碼"""
        token = create_access_token({"employee_id": "E001"})
        payload = decode_token(token)
        assert payload["employee_id"] == "E001"

    def test_invalid_token(self):
        """無效 token 拋出 401"""
        with pytest.raises(HTTPException) as exc_info:
            decode_token("invalid.token.here")
        assert exc_info.value.status_code == 401

    def test_expired_token(self):
        """過期 token 拋出 401"""
        token = create_access_token(
            {"id": "1"},
            expires_delta=timedelta(seconds=-1)
        )
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401
