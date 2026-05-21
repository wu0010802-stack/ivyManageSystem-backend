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
    assert s.database_url == "postgresql://localhost:5432/ivymanagement"
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
