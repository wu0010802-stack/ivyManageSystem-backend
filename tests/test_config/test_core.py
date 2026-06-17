import pytest
from config.core import CoreSettings


def test_defaults(monkeypatch):
    """全部 env 清空時應給 development default."""
    for var in (
        "ENV",
        "DATABASE_URL",
        "JWT_SECRET_KEY",
        "ENABLE_API_DOCS",
        "ADMIN_INIT_USERNAME",
        "ADMIN_INIT_PASSWORD",
        "JWT_ABSOLUTE_LIFETIME_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = CoreSettings()
    assert s.env == "development"
    assert (
        s.database_url is None
    )  # dev fallback handled by models/base.py, not settings
    assert s.enable_api_docs is False
    assert s.jwt_absolute_lifetime_hours == 8
    assert s.admin_init_username is None
    assert s.admin_init_password is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    monkeypatch.setenv("JWT_SECRET_KEY", "supersecret")
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    monkeypatch.setenv("JWT_ABSOLUTE_LIFETIME_HOURS", "12")
    s = CoreSettings()
    assert s.env == "production"
    assert s.database_url == "postgresql://prod/db"
    assert s.jwt_secret_key == "supersecret"
    assert s.enable_api_docs is True
    assert s.jwt_absolute_lifetime_hours == 12


def test_is_production_property(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert CoreSettings().is_production is True
    monkeypatch.setenv("ENV", "prod")
    assert CoreSettings().is_production is True
    monkeypatch.setenv("ENV", "development")
    assert CoreSettings().is_production is False
    monkeypatch.setenv("ENV", "")
    assert CoreSettings().is_production is False


def test_dev_router_enabled(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    assert CoreSettings().dev_router_enabled is True
    monkeypatch.setenv("ENV", "test")
    assert CoreSettings().dev_router_enabled is True
    monkeypatch.setenv("ENV", "production")
    assert CoreSettings().dev_router_enabled is False


def test_docs_enabled_fail_closed(monkeypatch):
    """C30：docs/openapi 掛載改 fail-closed，僅 ENABLE_API_DOCS=true 才開。

    原 main.py `_docs_force_enable or not _is_prod_env` 為 fail-open：ENV 拼錯/
    漏設（非 production 字面）即自動開放 /docs /openapi.json 洩漏全 router 地圖。
    收緊為只看顯式 ENABLE_API_DOCS。
    """
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)

    # 非標準 ENV（typo / staging）未顯式開 docs → 不掛載
    monkeypatch.setenv("ENV", "staging")
    assert CoreSettings().docs_enabled is False
    monkeypatch.setenv("ENV", "pruduction")  # typo
    assert CoreSettings().docs_enabled is False

    # 連 development 未顯式設旗標也不開（fail-closed）
    monkeypatch.setenv("ENV", "development")
    assert CoreSettings().docs_enabled is False

    # 未設 ENV → 不開
    monkeypatch.delenv("ENV", raising=False)
    assert CoreSettings().docs_enabled is False

    # 顯式開啟才掛載（dev 看 docs 需顯式 ENABLE_API_DOCS=true）
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    assert CoreSettings().docs_enabled is True

    # prod 即使顯式開也允許（運維可控）
    monkeypatch.setenv("ENV", "production")
    assert CoreSettings().docs_enabled is True
