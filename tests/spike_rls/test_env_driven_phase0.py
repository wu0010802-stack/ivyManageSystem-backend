"""Phase 0 env-driven wiring tests.

Layer above tests/spike_rls/test_rls_wiring.py (URL-explicit factories).
Verifies that PARENT_DB_USER + PARENT_DB_PASSWORD env vars correctly compose
into a working engine, and that the FastAPI-style dep wrapper fail-loud
when env unset.

Requires migration parlsr001 applied if running test_5 (otherwise it skips).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from models import parent_db


@pytest.fixture(autouse=True)
def _reset_singleton_before_and_after():
    """Each test starts with a clean singleton (no stale cached engine
    from previous test) and tears it down after."""
    parent_db.reset_parent_engine_for_tests()
    yield
    parent_db.reset_parent_engine_for_tests()


@pytest.fixture
def _clear_parent_env(monkeypatch):
    """Drop any PARENT_DB_* from env so we test the "unset" branch cleanly."""
    monkeypatch.delenv("PARENT_DB_USER", raising=False)
    monkeypatch.delenv("PARENT_DB_PASSWORD", raising=False)


# ---------------------------------------------------------------------------
# Pure URL composition
# ---------------------------------------------------------------------------


def test_build_url_returns_none_when_user_missing(_clear_parent_env, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    assert parent_db._build_parent_url_from_env() is None


def test_build_url_returns_none_when_password_missing(_clear_parent_env, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    assert parent_db._build_parent_url_from_env() is None


def test_build_url_returns_none_when_database_url_missing(
    _clear_parent_env, monkeypatch
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "x")
    assert parent_db._build_parent_url_from_env() is None


def test_build_url_overlays_credentials(_clear_parent_env, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "secret_pw")

    url = parent_db._build_parent_url_from_env()

    assert url == "postgresql://ivy_parent_login:secret_pw@localhost:5432/ivymanagement"


def test_build_url_preserves_port_when_present(_clear_parent_env, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://other_user:other_pw@db.example.com:6543/proddb?sslmode=require",
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "secret_pw")

    url = parent_db._build_parent_url_from_env()

    # Port + path + query preserved; only user:pw replaced
    assert url == (
        "postgresql://ivy_parent_login:secret_pw@db.example.com:6543/proddb?sslmode=require"
    )


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_get_parent_engine_returns_none_when_env_unset(_clear_parent_env):
    assert parent_db.get_parent_engine() is None


def test_get_parent_engine_returns_engine_when_env_set(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "dummy_pw_not_used_for_lazy_connect")

    eng = parent_db.get_parent_engine()

    assert eng is not None
    # SQLAlchemy lazy connect — engine creation doesn't actually open conn.
    # Verify the url got composed correctly.
    assert eng.url.username == "ivy_parent_login"
    assert eng.url.host == "localhost"
    assert eng.url.port == 5432


def test_get_parent_engine_caches_singleton(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "dummy")

    eng1 = parent_db.get_parent_engine()
    eng2 = parent_db.get_parent_engine()

    assert eng1 is eng2, "subsequent calls must return the same Engine instance"


def test_reset_for_tests_clears_singleton(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "dummy")

    eng1 = parent_db.get_parent_engine()
    parent_db.reset_parent_engine_for_tests()
    eng2 = parent_db.get_parent_engine()

    assert eng1 is not eng2, "reset must force a fresh Engine on next call"


# ---------------------------------------------------------------------------
# FastAPI-style dep fail-loud behavior
# ---------------------------------------------------------------------------


def test_session_dep_raises_when_env_unset(_clear_parent_env):
    """RuntimeError, NOT silent fall-through to admin engine. This is the
    explicit Phase 1 invariant — if you forget to set PARENT_DB_USER in prod,
    the app must die loudly, not bypass RLS."""
    gen = parent_db.get_parent_session_dep(user_id=999)
    with pytest.raises(RuntimeError, match="Parent RLS engine not configured"):
        next(gen)


# ---------------------------------------------------------------------------
# End-to-end with real migration role (skip if migration not applied)
# ---------------------------------------------------------------------------


def _migration_role_with_password_works() -> bool:
    """Skip helper: is ivy_parent_login usable from this dev DB?
    Migration parlsr001 must be applied AND ops/test must have set the password
    with `ALTER ROLE ivy_parent_login PASSWORD 'dev_parent_pw_2026_05_18'`."""
    try:
        eng = create_engine(
            "postgresql://ivy_parent_login:dev_parent_pw_2026_05_18@localhost:5432/ivymanagement",
            pool_pre_ping=False,
        )
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _migration_role_with_password_works(),
    reason="ivy_parent_login unreachable (migration parlsr001 not applied OR "
    "password not set to dev_parent_pw_2026_05_18)",
)
def test_session_dep_applies_set_local_via_env_driven_engine(monkeypatch):
    """Full env→engine→dep→SET LOCAL chain with the real migration role.

    Doesn't need any GRANT or RLS policy because we only verify
    `current_setting('app.current_user_id')` round-trips correctly. RLS
    isolation is covered by tests/spike_rls/test_rls_wiring.py."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://yilunwu@localhost:5432/ivymanagement"
    )
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "dev_parent_pw_2026_05_18")

    gen = parent_db.get_parent_session_dep(user_id=4242)
    session = next(gen)
    try:
        result = session.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        assert (
            result == "4242"
        ), f"expected SET LOCAL to set user_id=4242, got {result!r}"
    finally:
        # Exhaust generator (commit + close)
        for _ in gen:
            pass
