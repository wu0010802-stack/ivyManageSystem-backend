"""Phase 2a: parent_engine before_cursor_execute guard.

Validates the optional fail-loud guard installed when
`PARENT_RLS_GUARD_ENABLED=1`:
- Normal flow via build_parent_session_for_user: guard quiet (positive)
- Raw engine.connect() without SET LOCAL: guard raises RuntimeError (negative)
- Control statements (SET / COMMIT / ROLLBACK) don't trigger the guard
- Guard's own SELECT current_setting doesn't recurse
- env var off: guard not installed, fail-closed policy still gives 0 row
"""

from __future__ import annotations

import os
from typing import Generator

import pytest
from sqlalchemy import text

from models import parent_db

_PARENT_LOGIN_PW = "dev_parent_pw_2026_05_18"
_ADMIN_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"


def _login_works() -> bool:
    try:
        from sqlalchemy import create_engine

        eng = create_engine(
            f"postgresql://ivy_parent_login:{_PARENT_LOGIN_PW}@localhost:5432/ivymanagement",
            pool_pre_ping=False,
        )
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _login_works(),
    reason="ivy_parent_login unreachable",
)


_PARENT_URL = (
    f"postgresql://ivy_parent_login:{_PARENT_LOGIN_PW}@localhost:5432/ivymanagement"
)


@pytest.fixture
def guard_engine():
    """Build a fresh parent_engine with the guard installed (forces env on)."""
    os.environ["PARENT_RLS_GUARD_ENABLED"] = "1"
    from config import reset_for_tests

    reset_for_tests()
    try:
        eng = parent_db.get_parent_engine_for_url(_PARENT_URL)
        yield eng
    finally:
        eng.dispose()
        os.environ.pop("PARENT_RLS_GUARD_ENABLED", None)
        from config import reset_for_tests

        reset_for_tests()


@pytest.fixture
def no_guard_engine():
    """Build a fresh parent_engine WITHOUT the guard (env off)."""
    os.environ.pop("PARENT_RLS_GUARD_ENABLED", None)
    from config import reset_for_tests

    reset_for_tests()
    eng = parent_db.get_parent_engine_for_url(_PARENT_URL)
    try:
        yield eng
    finally:
        eng.dispose()
        from config import reset_for_tests

        reset_for_tests()


# ---------------------------------------------------------------------------
# Positive: normal flow via build_parent_session_for_user → guard quiet
# ---------------------------------------------------------------------------


def test_guard_quiet_under_normal_flow(guard_engine):
    """build_parent_session_for_user sets SET LOCAL inside the tx, so handler
    queries see app.current_user_id and the guard passes silently."""
    gen = parent_db.build_parent_session_for_user(guard_engine, user_id=42)
    session = next(gen)
    try:
        # Execute a real query that depends on RLS context — guard should NOT
        # fire because SET LOCAL has set app.current_user_id to '42'
        val = session.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        assert val == "42"
    finally:
        for _ in gen:
            pass


# ---------------------------------------------------------------------------
# Negative: raw engine.connect() bypasses SET LOCAL → guard fires
# ---------------------------------------------------------------------------


def test_guard_raises_when_app_user_id_not_set(guard_engine):
    """A caller that grabs engine.connect() directly (skipping the dep) has
    no SET LOCAL in the new tx. The guard catches this with RuntimeError."""
    with guard_engine.connect() as conn:
        with pytest.raises(RuntimeError, match="without app.current_user_id"):
            conn.execute(text("SELECT 1 FROM student_attendances LIMIT 1"))


def test_guard_message_includes_statement_preview(guard_engine):
    """Error message helps diagnose by including the offending statement."""
    with guard_engine.connect() as conn:
        with pytest.raises(RuntimeError) as exc:
            conn.execute(text("SELECT id FROM guardians WHERE user_id = 1"))
    assert "SELECT id FROM guardians" in str(exc.value)


# ---------------------------------------------------------------------------
# Control statements skipped (no guard interference)
# ---------------------------------------------------------------------------


def test_guard_skips_set_and_reset(guard_engine):
    """SET / RESET / SHOW statements set/inspect the context itself — guard
    must not require app.current_user_id to be set when issuing them, else
    we'd never be able to bootstrap the context."""
    with guard_engine.connect() as conn:
        # These should all execute without RuntimeError
        conn.execute(text("SET app.current_user_id = '99'"))
        conn.execute(text("SHOW search_path"))
        conn.execute(text("RESET app.current_user_id"))
        # SET-then-SELECT also fine since the SET set the var
        conn.execute(text("SET app.current_user_id = '99'"))
        val = conn.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        assert val == "99"


def test_guard_skips_commit_and_rollback(guard_engine):
    """COMMIT / ROLLBACK / BEGIN / SAVEPOINT are tx control, not data —
    guard ignores them."""
    with guard_engine.connect() as conn:
        # SQLAlchemy 2.x uses implicit BEGIN; explicit BEGIN/COMMIT round-trip
        # happens via begin()/commit() on connection.
        with conn.begin() as tx:
            conn.execute(text("SET LOCAL app.current_user_id = '7'"))
            # Now a real query is OK since SET LOCAL set the var
            val = conn.execute(
                text("SELECT current_setting('app.current_user_id', true)")
            ).scalar()
            assert val == "7"
        # tx.commit() called; SET LOCAL gone, but commit itself didn't raise


# ---------------------------------------------------------------------------
# Recursion: guard's own current_setting query doesn't fire the guard again
# ---------------------------------------------------------------------------


def test_guard_does_not_recurse(guard_engine):
    """Sanity: under normal flow with N statements, guard runs N times not 2^N.
    We can't easily count here, but we can verify the test suite as a whole
    runs in reasonable time without explosion. The threading.local recursion
    flag is the actual mechanism — proven correct by absence of stack overflow."""
    gen = parent_db.build_parent_session_for_user(guard_engine, user_id=42)
    session = next(gen)
    try:
        # 10 sequential queries — if recursion existed, each call would 2x
        for i in range(10):
            session.execute(text("SELECT :i"), {"i": i}).scalar()
    finally:
        for _ in gen:
            pass


# ---------------------------------------------------------------------------
# Env var off: guard not installed, fail-closed RLS policy still gives 0 row
# ---------------------------------------------------------------------------


def test_guard_off_falls_back_to_fail_closed_policy(no_guard_engine):
    """Without the guard, raw queries return 0 row (via NULLIF in policy
    USING clause) instead of raising. This is the policy-layer baseline."""
    with no_guard_engine.connect() as conn:
        rows = conn.execute(text("SELECT 1 FROM student_attendances LIMIT 1")).all()
        # Either 0 rows (RLS fail-closed) or permission denied — neither
        # should be a RuntimeError from our guard
        assert rows == []
