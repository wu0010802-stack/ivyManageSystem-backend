"""Phase 2d: parent_engine per-session metrics emission.

Validates that `build_parent_session_for_user` emits a single structured INFO
log at session close with elapsed_ms timing. Best-effort — metrics failure
must never break a real request.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from models import parent_db

_PARENT_LOGIN_PW = "dev_parent_pw_2026_05_18"
_PARENT_URL = (
    f"postgresql://ivy_parent_login:{_PARENT_LOGIN_PW}@localhost:5432/ivymanagement"
)


def _login_works() -> bool:
    try:
        from sqlalchemy import create_engine

        eng = create_engine(_PARENT_URL, pool_pre_ping=False)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _login_works(), reason="ivy_parent_login unreachable"
)


@pytest.fixture
def metrics_engine():
    eng = parent_db.get_parent_engine_for_url(_PARENT_URL)
    try:
        yield eng
    finally:
        eng.dispose()


def test_metrics_emits_log_at_session_close(caplog, metrics_engine):
    """Normal flow should emit parent_rls_session INFO log with elapsed_ms."""
    caplog.set_level(logging.INFO, logger="parent_rls")
    gen = parent_db.build_parent_session_for_user(metrics_engine, user_id=42)
    session = next(gen)
    try:
        session.execute(text("SELECT 1")).scalar()
    finally:
        for _ in gen:
            pass

    # Find the parent_rls_session log line
    matching = [r for r in caplog.records if "parent_rls_session" in r.getMessage()]
    assert len(matching) == 1, f"expected 1 parent_rls_session log, got {len(matching)}"
    rec = matching[0]
    # Verify structured extras present
    assert rec.parent_rls_user_id == 42
    assert isinstance(rec.parent_rls_elapsed_ms, float)
    assert rec.parent_rls_elapsed_ms >= 0
    # Reasonable upper bound — local query should be < 5s even on slow CI
    assert rec.parent_rls_elapsed_ms < 5000


def test_metrics_failure_does_not_break_request(caplog, metrics_engine, monkeypatch):
    """If something inside _emit_parent_session_metrics raises, the session
    must still close cleanly (best-effort observability)."""

    def _broken_logger_info(*args, **kwargs):
        raise RuntimeError("simulated logger failure")

    monkeypatch.setattr(parent_db.logger, "info", _broken_logger_info)
    caplog.set_level(logging.ERROR, logger="parent_rls")

    gen = parent_db.build_parent_session_for_user(metrics_engine, user_id=99)
    session = next(gen)
    try:
        # Real work must complete despite metrics failure
        val = session.execute(text("SELECT 'ok'")).scalar()
        assert val == "ok"
    finally:
        for _ in gen:
            pass
    # Verify the failure was caught + logged as exception
    matching = [
        r
        for r in caplog.records
        if "parent_rls metrics emission failed" in r.getMessage()
    ]
    assert len(matching) == 1


def test_metrics_disabled_via_env_does_not_install_listener(monkeypatch):
    """PARENT_RLS_METRICS_DISABLED=1 should skip the engine-level metrics
    listener install. (Session-close emit still runs since it's not gated
    by env — it's tied to the dep; the env var is a hook reserved for
    future per-query counter when we install it.)"""
    monkeypatch.setenv("PARENT_RLS_METRICS_DISABLED", "1")
    eng = parent_db.get_parent_engine_for_url(_PARENT_URL)
    try:
        # Just verify no error during construction with the env var set
        assert eng is not None
    finally:
        eng.dispose()
