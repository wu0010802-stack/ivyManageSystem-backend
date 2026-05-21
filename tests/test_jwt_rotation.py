"""
JWT secret rotation 測試：kid header + 多 key 容忍。

對應 spec：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
"""

import hashlib
import os
import importlib
import pytest

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
