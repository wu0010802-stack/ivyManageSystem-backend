"""
JWT secret rotation 測試：kid header + 多 key 容忍。

對應 spec：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
"""

import hashlib
import os
import importlib
import pytest
from fastapi import HTTPException

import utils.auth as _auth_module_for_reset

# Capture the original JWT_SECRET_KEY once at module import time, before any
# test reloads utils.auth.  If JWT_SECRET_KEY is not set in the environment
# the module generates a random secret on each reload, so we must pin the env
# var to the original value rather than rely on a fresh reload.
_ORIGINAL_JWT_SECRET_KEY: str = _auth_module_for_reset.JWT_SECRET_KEY


@pytest.fixture(autouse=True)
def _reset_auth_module_after_test():
    """Tests in this file reload utils.auth with mutated env vars.
    monkeypatch restores env vars after each test, but the module remains
    contaminated.  We pin the original JWT_SECRET_KEY into the env ourselves
    (using os.environ directly, so we control timing) and clean up any
    rotation-specific vars before reloading, ensuring later test files see
    pristine module state.

    We pin the original secret rather than relying on a bare reload because
    JWT_SECRET_KEY may not be set in the dev environment, causing each reload
    to produce a different random secret.
    """
    yield
    # Explicitly set the original secret and clear any vars the prod-error
    # tests may have left behind (monkeypatch teardown ordering is not
    # guaranteed relative to this fixture).
    os.environ["JWT_SECRET_KEY"] = _ORIGINAL_JWT_SECRET_KEY
    os.environ.pop("JWT_SECRET_KEYS_OLDS", None)
    os.environ.pop("ENV", None)
    importlib.reload(_auth_module_for_reset)


def _expected_kid(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _reload_auth(
    monkeypatch, *, current: str, olds: str | None = None, env: str = "development"
):
    """重新載入 utils.auth 以套用環境變數。回傳 reloaded module。"""
    monkeypatch.setenv("JWT_SECRET_KEY", current)
    monkeypatch.setenv("ENV", env)
    if olds is None:
        monkeypatch.delenv("JWT_SECRET_KEYS_OLDS", raising=False)
    else:
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", olds)
    import utils.auth as auth_module

    return importlib.reload(auth_module)


class TestKidDerivation:

    def test_kid_is_sha256_truncated_to_12_hex(self, monkeypatch):
        """kid 必為 sha256(secret) 前 12 hex chars"""
        secret = "current-secret-v1"
        auth = _reload_auth(monkeypatch, current=secret)
        assert auth._CURRENT_KID == _expected_kid(secret)
        assert len(auth._CURRENT_KID) == 12

    def test_kid_changes_with_secret(self, monkeypatch):
        """不同 secret 產生不同 kid"""
        a = _reload_auth(monkeypatch, current="aaa")
        kid_a = a._CURRENT_KID
        b = _reload_auth(monkeypatch, current="bbb")
        kid_b = b._CURRENT_KID
        assert kid_a != kid_b


class TestVerifyKeysLoading:

    def test_only_current_when_olds_empty(self, monkeypatch):
        """無 olds 時，_VERIFY_KEYS 只含 current"""
        auth = _reload_auth(monkeypatch, current="cur1")
        assert auth._VERIFY_KEYS == {_expected_kid("cur1"): "cur1"}
        assert auth._LEGACY_TRY_ORDER == ["cur1"]

    def test_olds_appended_to_verify_keys(self, monkeypatch):
        """olds 中所有 key 都進 _VERIFY_KEYS"""
        auth = _reload_auth(
            monkeypatch,
            current="cur2",
            olds='["old_a","old_b"]',
        )
        assert auth._VERIFY_KEYS == {
            _expected_kid("cur2"): "cur2",
            _expected_kid("old_a"): "old_a",
            _expected_kid("old_b"): "old_b",
        }
        assert auth._LEGACY_TRY_ORDER == ["cur2", "old_a", "old_b"]

    def test_empty_string_olds_skipped(self, monkeypatch):
        """olds 中空字串會被略過"""
        auth = _reload_auth(monkeypatch, current="cur3", olds='["", "real_old"]')
        assert _expected_kid("real_old") in auth._VERIFY_KEYS
        # 空字串不進 _VERIFY_KEYS 也不進 _LEGACY_TRY_ORDER
        assert "" not in auth._LEGACY_TRY_ORDER
        assert auth._LEGACY_TRY_ORDER == ["cur3", "real_old"]


class TestOldsParsing:

    def test_invalid_json_dev_falls_back_to_empty(self, monkeypatch):
        """dev 模式 JSON 損毀 → warning + 視為空 list"""
        auth = _reload_auth(
            monkeypatch, current="cur", olds="not-json", env="development"
        )
        assert auth._olds == []

    def test_invalid_json_prod_raises(self, monkeypatch):
        """prod 模式 JSON 損毀 → RuntimeError"""
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "cur")
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", "not-json")
        import utils.auth as auth_module

        with pytest.raises(RuntimeError, match="JWT_SECRET_KEYS_OLDS"):
            importlib.reload(auth_module)

    def test_non_list_value_rejected_in_prod(self, monkeypatch):
        """olds 不是 list（例：dict）→ prod fail-loud"""
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "cur")
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", '{"v1":"x"}')
        import utils.auth as auth_module

        with pytest.raises(RuntimeError, match="list of strings"):
            importlib.reload(auth_module)


class TestSignIncludesKid:

    def test_create_access_token_includes_kid_header(self, monkeypatch):
        """新簽 token 的 header 必含 kid，值等於 _CURRENT_KID"""
        from jose import jwt as jose_jwt

        auth = _reload_auth(monkeypatch, current="signing-cur")
        token = auth.create_access_token({"user_id": 1})
        header = jose_jwt.get_unverified_header(token)
        assert header["kid"] == auth._CURRENT_KID
        assert header["alg"] == "HS256"

    def test_kid_in_header_matches_secret_used_to_sign(self, monkeypatch):
        """kid 確實對應簽章 secret（用該 kid 對應的 secret 能驗過）"""
        from jose import jwt as jose_jwt

        auth = _reload_auth(monkeypatch, current="match-test")
        token = auth.create_access_token({"user_id": 1})
        header = jose_jwt.get_unverified_header(token)
        kid = header["kid"]
        secret = auth._VERIFY_KEYS[kid]
        payload = jose_jwt.decode(token, secret, algorithms=["HS256"])
        assert payload["user_id"] == 1


# ── Task 3 helpers ────────────────────────────────────────────────────────────


def _craft_token(header: dict, payload: dict, secret: str) -> str:
    """手動組 token：用指定 secret 簽，header 可任意指定（含 kid 偽造）"""
    from jose import jwt as jose_jwt

    return jose_jwt.encode(payload, secret, algorithm="HS256", headers=header)


def _craft_legacy_token_no_kid(payload: dict, secret: str) -> str:
    """模擬升版前舊 token：用指定 secret 簽，header 不帶 kid"""
    from jose import jwt as jose_jwt

    # python-jose 預設 header 不含 kid（除非顯式傳 headers）
    return jose_jwt.encode(payload, secret, algorithm="HS256")


class TestVerifyMultiKey:

    def test_verify_with_current_key(self, monkeypatch):
        """current 簽 + current 驗 → pass"""
        auth = _reload_auth(monkeypatch, current="cur-v1")
        token = auth.create_access_token({"user_id": 1})
        payload = auth.decode_token(token)
        assert payload["user_id"] == 1

    def test_verify_with_old_kid_in_olds_list(self, monkeypatch):
        """舊 key 簽（kid_old）→ 啟動時 olds 含舊 key → 驗 pass"""
        # 模擬：rotation 進行中，舊 token 仍在外
        auth = _reload_auth(monkeypatch, current="new-key", olds='["old-key"]')
        old_kid = _expected_kid("old-key")
        # 用 old-key 簽，header 帶 old kid
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 7, "exp": 9999999999},
            "old-key",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 7

    def test_verify_with_old_kid_not_in_olds(self, monkeypatch):
        """舊 key 簽 + olds 不含 → 401（rotation 完成後預期）"""
        auth = _reload_auth(monkeypatch, current="new-key")  # olds 為空
        old_kid = _expected_kid("removed-old")
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 7, "exp": 9999999999},
            "removed-old",
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401

    def test_verify_unknown_kid_rejected(self, monkeypatch):
        """偽造 kid header（從未在 _VERIFY_KEYS 中）→ 401"""
        auth = _reload_auth(monkeypatch, current="cur-v2")
        token = _craft_token(
            {"alg": "HS256", "kid": "deadbeefcafe"},
            {"user_id": 1, "exp": 9999999999},
            "cur-v2",  # 即使簽章用對的 secret，未知 kid 也拒絕
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401


class TestVerifyLegacyNoKid:

    def test_legacy_no_kid_verified_by_current(self, monkeypatch):
        """升版前 token（無 kid，用 current 簽）→ legacy try-loop 試到 current → pass"""
        auth = _reload_auth(monkeypatch, current="cur-legacy")
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "cur-legacy",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 9

    def test_legacy_no_kid_verified_by_old_in_olds(self, monkeypatch):
        """升版前舊 token，secret 已 rotate；olds 仍含舊 key → legacy try-loop pass"""
        auth = _reload_auth(monkeypatch, current="new", olds='["pre-rotate"]')
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "pre-rotate",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 9

    def test_legacy_no_kid_unknown_key_rejected(self, monkeypatch):
        """升版前 token，secret 早被刪 → legacy try-loop 全失敗 → 401"""
        auth = _reload_auth(monkeypatch, current="new")  # olds 空
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "long-gone-secret",
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401


class TestDecodeTokenAllowExpiredMultiKey:

    def test_allow_expired_with_current_kid(self, monkeypatch):
        """current 簽的過期 token 在 grace 內 → decode_token_allow_expired pass"""
        import time

        auth = _reload_auth(monkeypatch, current="cur-grace")
        # 過期 30 秒（在 2h grace 內）
        past_exp = int(time.time()) - 30
        token = _craft_token(
            {"alg": "HS256", "kid": auth._CURRENT_KID},
            {"user_id": 1, "exp": past_exp},
            "cur-grace",
        )
        payload = auth.decode_token_allow_expired(token)
        assert payload["user_id"] == 1

    def test_allow_expired_with_old_kid(self, monkeypatch):
        """rotation 期間 olds 中舊 key 簽的過期 token 也能在 grace 內 decode"""
        import time

        auth = _reload_auth(monkeypatch, current="new", olds='["old-grace"]')
        old_kid = _expected_kid("old-grace")
        past_exp = int(time.time()) - 30
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 1, "exp": past_exp},
            "old-grace",
        )
        payload = auth.decode_token_allow_expired(token)
        assert payload["user_id"] == 1

    def test_allow_expired_beyond_grace_raises_401(self, monkeypatch):
        """multi-key 路徑下，超出 grace 的過期 token → 401"""
        import time

        auth = _reload_auth(monkeypatch, current="grace-exceed")
        past_exp = int(time.time()) - (auth.JWT_REFRESH_GRACE_HOURS * 3600 + 60)
        token = _craft_token(
            {"alg": "HS256", "kid": auth._CURRENT_KID},
            {"user_id": 1, "exp": past_exp},
            "grace-exceed",
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token_allow_expired(token)
        assert excinfo.value.status_code == 401
        assert "刷新期限" in excinfo.value.detail
