import pytest


def test_settings_composes_sub_settings(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    from config import reset_for_tests, get_settings

    reset_for_tests()
    s = get_settings()
    assert s.core.env == "production"
    assert s.core.is_production is True
    assert s.sentry.dsn == "https://abc@sentry.io/1"
    assert s.sentry.enabled is True


def test_get_settings_singleton():
    from config import reset_for_tests, get_settings

    reset_for_tests()
    assert get_settings() is get_settings()


def test_reset_for_tests_clears_cache(monkeypatch):
    from config import reset_for_tests, get_settings

    monkeypatch.setenv("ENV", "development")
    reset_for_tests()
    a = get_settings()
    assert a.core.env == "development"
    monkeypatch.setenv("ENV", "production")
    reset_for_tests()
    b = get_settings()
    assert b.core.env == "production"
    assert a is not b


def test_model_dump_safe_redacts_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "supersecret")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "p4ssw0rd")
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza...")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    from config import reset_for_tests, get_settings

    reset_for_tests()
    dumped = get_settings().model_dump_safe()
    # 含敏感 substring 的欄位被 redact
    assert dumped["core"]["jwt_secret_key"] == "***"
    assert dumped["parent_db"]["password"] == "***"
    assert dumped["sentry"]["dsn"] == "***"
    assert dumped["geocoding"]["google_maps_api_key"] == "***"
    # database_url 含 'url' 不在 denylist，照常出（雖然字串裡有密碼但 substring 不匹配 'url'）
    # 非敏感欄位照常出
    assert (
        dumped["core"]["env"] == "development" or dumped["core"]["env"] == "production"
    )


def test_model_dump_safe_preserves_none(monkeypatch):
    """敏感欄位若值為 None 不要 redact 成 '***'，方便 debug 看出未設。"""
    for var in ("JWT_SECRET_KEY", "PARENT_DB_PASSWORD", "SENTRY_DSN"):
        monkeypatch.delenv(var, raising=False)
    from config import reset_for_tests, get_settings

    reset_for_tests()
    dumped = get_settings().model_dump_safe()
    assert dumped["core"]["jwt_secret_key"] is None
    assert dumped["parent_db"]["password"] is None
    assert dumped["sentry"]["dsn"] is None


def test_model_dump_safe_exempts_known_false_positives(monkeypatch):
    """activity_query_token_ttl_days 含 'token' substring 但不是敏感欄位（天數常數）。

    透過 _SENSITIVE_KEY_EXEMPT 確保此欄位的值正常出現在 dump 中，方便 debug 看到實際 TTL。
    """
    monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "60")
    from config import reset_for_tests, get_settings

    reset_for_tests()
    dumped = get_settings().model_dump_safe()
    assert (
        dumped["misc"]["activity_query_token_ttl_days"] == 60
    ), "activity_query_token_ttl_days 不該被 redact（_SENSITIVE_KEY_EXEMPT 失效）"
