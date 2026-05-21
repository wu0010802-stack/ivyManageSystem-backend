import pytest
from config.parent_db import ParentDBSettings


def test_defaults(monkeypatch):
    for var in (
        "PARENT_DB_USER",
        "PARENT_DB_PASSWORD",
        "PARENT_RLS_GUARD_ENABLED",
        "PARENT_RLS_METRICS_DISABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    s = ParentDBSettings()
    assert s.user is None
    assert s.password is None
    assert s.rls_guard_enabled is False
    assert s.rls_metrics_disabled is False


def test_env_reads(monkeypatch):
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "secret")
    monkeypatch.setenv("PARENT_RLS_GUARD_ENABLED", "true")
    monkeypatch.setenv("PARENT_RLS_METRICS_DISABLED", "1")
    s = ParentDBSettings()
    assert s.user == "ivy_parent_login"
    assert s.password == "secret"
    assert s.rls_guard_enabled is True
    assert s.rls_metrics_disabled is True
