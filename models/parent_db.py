"""Parent portal isolated DB engine — used by api/parent_portal/* routers.

Two layers:
- **URL-explicit factories** (`get_parent_engine_for_url` /
  `get_admin_engine_for_url` / `build_parent_session_for_user`) take URL/params
  directly; mainly for tests where credentials come from a fixture, not env.
- **Env-driven singletons** (`get_parent_engine` / `get_parent_session_dep`)
  read `PARENT_DB_USER` + `PARENT_DB_PASSWORD` and overlay them on
  `DATABASE_URL` to derive the parent-role connection. Production code uses
  these via a FastAPI dependency that injects `user_id` from `get_current_user`.

Phase 0 (2026-05-18, parlsr001 migration) creates the four roles and the
guardians partial index, but does NOT enable RLS on any table and does NOT
switch any router. So `get_parent_engine()` may return a working Engine, but
trying to query `public.*` through it will fail with permission-denied until
Phase 1 wires GRANTs + ENABLE RLS atomically per table.

Spike status (2026-05-18): URL-explicit wiring + policy proven against
`rls_spike` schema in tests/spike_rls/. Env-driven path proven by phase 0
tests using the migration-created roles.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Generator
from urllib.parse import urlparse, urlunparse

from config import get_settings

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("parent_rls")

# Thread-local recursion guard for the optional before_cursor_execute check.
# Set when the guard is itself querying current_setting(); cleared after.
# Without this, the guard's own SELECT would recursively trigger the listener
# and infinite-loop.
_guard_recursion = threading.local()

# ---------------------------------------------------------------------------
# Engine factories (test-friendly: accept URL directly so we can inject test
# credentials without env-var gymnastics; production wrappers in Phase 0).
# ---------------------------------------------------------------------------


def get_parent_engine_for_url(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_pre_ping: bool = True,
    pool_recycle: int = 1800,
) -> Engine:
    """Build a SQLAlchemy engine bound to the RLS-enforced parent login role.

    Installs a `connect` event listener that resets `app.current_user_id` to
    empty string when each physical connection is established. This is a
    defensive baseline — the real isolation comes from `SET LOCAL` inside
    each request's tx. The reset matters only for code paths that bypass
    `build_parent_session_for_user` (e.g., a raw `engine.connect()` for
    diagnostic queries); those still see 0 rows under fail-closed policies.

    Note on Supabase Transaction Mode (port 6543): the connect event fires
    when a physical backend is bound to a pooled client, NOT on every logical
    checkout. Defensive baseline weakens there, but `SET LOCAL` semantics
    remain correct (it's tx-scoped, not session-scoped). See design doc §3.1.
    """
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=pool_pre_ping,
        pool_recycle=pool_recycle,
        connect_args={"options": "-c statement_timeout=30000"},
    )

    @event.listens_for(engine, "connect")
    def _reset_app_var(dbapi_connection, _connection_record):
        with dbapi_connection.cursor() as cur:
            cur.execute("SET app.current_user_id = ''")

    # Optional fail-loud guard: every regular query verifies app.current_user_id
    # is set. Cheap defense-in-depth that catches "forgot to switch to
    # parent_engine via build_parent_session_for_user" mistakes immediately
    # (vs silently returning 0 rows via fail-closed policy).
    #
    # Gated by env var to keep prod fast-path quiet — RLS policies are still
    # the data-layer source of truth; guard is a developer-experience aid.
    if get_settings().parent_db.rls_guard_enabled:
        _install_parent_engine_guard(engine)

    # Per-session metrics: count queries + total elapsed via dbapi-level
    # before_cursor_execute listener. Always-on by default (cheap; just a
    # counter increment per query). Disable with PARENT_RLS_METRICS_DISABLED=1
    # if you have a hot path that genuinely doesn't want any listener overhead.
    if not get_settings().parent_db.rls_metrics_disabled:
        _install_parent_engine_metrics(engine)

    return engine


_GUARD_SKIP_PREFIXES = (
    "SET ",
    "RESET ",
    "COMMIT",
    "ROLLBACK",
    "BEGIN",
    "SAVEPOINT",
    "RELEASE",
    "DISCARD",
    "SHOW ",
)


def _install_parent_engine_guard(engine: Engine) -> None:
    """Install a `before_cursor_execute` listener that asserts
    `app.current_user_id` is set on every non-control statement.

    Failure mode is RuntimeError raised inline — caller's exception path runs
    rather than silently 0-row-fail-closed. Useful in dev/test, optional in prod.

    Uses a thread-local recursion flag to skip the listener while it's running
    its own `current_setting` check (otherwise infinite-loop).
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _ensure_app_user_id_set(
        conn, cursor, statement, parameters, context, executemany
    ):
        # Skip recursion: our own check below issues SELECT current_setting()
        if getattr(_guard_recursion, "checking", False):
            return

        # Skip control / metadata statements that don't need RLS context
        stripped = statement.lstrip().upper()
        for prefix in _GUARD_SKIP_PREFIXES:
            if stripped.startswith(prefix):
                return

        # Side-channel check via dbapi cursor (avoids SQLAlchemy round-trip
        # & thus avoids recursion overhead on the wrapped Connection)
        _guard_recursion.checking = True
        try:
            check_cur = conn.connection.cursor()
            try:
                check_cur.execute("SELECT current_setting('app.current_user_id', true)")
                row = check_cur.fetchone()
                val = row[0] if row else None
            finally:
                check_cur.close()
        finally:
            _guard_recursion.checking = False

        if not val:
            preview = (statement or "")[:120].replace("\n", " ")
            raise RuntimeError(
                "parent_engine query without app.current_user_id set. "
                "Caller must go through build_parent_session_for_user() / "
                "get_parent_session_dep(). "
                f"Statement: {preview}..."
            )


def get_admin_engine_for_url(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_pre_ping: bool = True,
    pool_recycle: int = 1800,
) -> Engine:
    """Build a SQLAlchemy engine bound to the BYPASSRLS admin login role.

    No connect listener — admin queries should always see the full table.
    """
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=pool_pre_ping,
        pool_recycle=pool_recycle,
        connect_args={"options": "-c statement_timeout=30000"},
    )


# ---------------------------------------------------------------------------
# Per-session metrics (Phase 2d): observability for the RLS overhead.
# Count queries + total elapsed in build_parent_session_for_user. Emit a
# single structured INFO log line at session close so prod can see whether
# parent_rls is doing 5 queries or 50 per request.
# ---------------------------------------------------------------------------


def _install_parent_engine_metrics(engine: Engine) -> None:
    """Currently a no-op for the engine; metrics emission happens at session
    close (`_emit_parent_session_metrics`) using a `session.info` timer.

    Per-session query-count tracking would require more glue with SQLAlchemy
    pool semantics (counter on raw_connection.info doesn't survive checkout/
    checkin cleanly). Skipped here in favor of elapsed-ms timing alone,
    which is the metric that matters for "is RLS hurting latency?".

    Reserved for future: install a `before_cursor_execute` listener that
    counts queries into `session.info` via SQLAlchemy 2.x context.session.
    """
    # Reserved hook — intentionally no-op for now.
    return


def _emit_parent_session_metrics(user_id: int, session: Session) -> None:
    """Emit a single structured INFO log at session close with the elapsed
    wall-clock time spent in the parent-scoped tx.

    Operators can grep / Sentry-filter for `parent_rls_session` to see
    p95/p99 of parent request latency at the DB layer; if it's >100ms with
    a single user_id, RLS JOINs are likely the cause — investigate the
    `ix_guardians_user_active` covering index hit rate.

    Failure here never breaks the request — observability is best-effort.
    """
    try:
        started_at = session.info.get("_parent_rls_started_at")
        elapsed_ms = (
            (time.monotonic() - started_at) * 1000 if started_at is not None else 0.0
        )
        logger.info(
            "parent_rls_session user_id=%s elapsed_ms=%.1f",
            user_id,
            elapsed_ms,
            extra={
                "parent_rls_user_id": user_id,
                "parent_rls_elapsed_ms": round(elapsed_ms, 1),
            },
        )
    except Exception:
        # Metric emission must never break a real request
        logger.exception("parent_rls metrics emission failed (non-fatal)")


# ---------------------------------------------------------------------------
# Post-commit callback queue
# ---------------------------------------------------------------------------
# Parent handlers MUST NOT call session.commit() (commit drops SET LOCAL → RLS
# isolation breaks). The commit happens in build_parent_session_for_user's
# `with session.begin():` block, AFTER the handler returns. Side effects that
# must observe committed data — e.g. report-cache invalidation — therefore can't
# run inline in the handler: doing so deletes/recomputes the cache while the
# parent write is still uncommitted, so a concurrent reader rebuilds the cache
# from pre-commit (stale) data and re-persists it for the full TTL. Queue such
# side effects here; they run only if the transaction commits (skipped on
# rollback). Mirrors the public/admin paths which invalidate AFTER session.commit().


def register_parent_post_commit(session: Session, callback) -> None:
    """Queue a zero-arg callable to run after the parent RLS tx commits."""
    session.info.setdefault("_post_commit_callbacks", []).append(callback)


def run_parent_post_commit_callbacks(session: Session) -> None:
    """Run + clear queued post-commit callbacks. Best-effort: a failing callback
    is logged and never propagated (a cache-invalidation glitch must not turn a
    successful, already-committed parent mutation into a 500)."""
    callbacks = session.info.get("_post_commit_callbacks") or []
    # Clear first so a re-entrant/duplicate run can't double-fire.
    session.info["_post_commit_callbacks"] = []
    for cb in callbacks:
        try:
            cb()
        except Exception:
            logger.exception("parent post-commit callback failed (non-fatal)")


# ---------------------------------------------------------------------------
# Generator-style session factory: yields a Session bound to a tx that has
# `SET LOCAL app.current_user_id = :uid` applied. The yield MUST stay inside
# the `with session.begin():` block — moving it outside silently breaks RLS
# isolation (the tx commits, SET LOCAL dies, next query sees 0 rows).
# ---------------------------------------------------------------------------


def build_parent_session_for_user(
    engine: Engine, user_id: int
) -> Generator[Session, None, None]:
    """Yield a session whose tx has `app.current_user_id` set to `user_id`.

    Usage in a FastAPI dependency:

        def get_parent_session_dep(
            current_user: dict = Depends(get_current_user),
        ):
            engine = get_parent_engine()  # singleton wrapper, Phase 0
            yield from build_parent_session_for_user(engine, current_user["user_id"])

    Inside the handler, **do not call session.commit()** — let the `with`
    block here own the commit. If you need to flush before a follow-up read,
    use `session.flush()`. Calling commit() ends the tx, which drops
    SET LOCAL; subsequent queries run with empty `app.current_user_id` and
    return 0 rows for every RLS-protected table.
    """
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    # Per-session counters for observability log; populated by the
    # before_cursor_execute listener installed below.
    session.info["_parent_rls_query_count"] = 0
    session.info["_parent_rls_started_at"] = time.monotonic()
    try:
        with session.begin():
            session.execute(
                text("SET LOCAL app.current_user_id = :uid"),
                {"uid": str(user_id)},
            )
            yield session
        # Tx committed without error → fire deferred side effects (e.g. cache
        # invalidation) now that the data is durable. Skipped if the `with`
        # block raised (rolled back), since control jumps to finally.
        run_parent_post_commit_callbacks(session)
        _emit_parent_session_metrics(user_id, session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Env-driven singletons (production path)
# ---------------------------------------------------------------------------

_parent_engine_lock = threading.Lock()
_parent_engine_instance: Engine | None = None


def _build_parent_url_from_env() -> str | None:
    """Overlay `PARENT_DB_USER` / `PARENT_DB_PASSWORD` credentials on `DATABASE_URL`.

    Returns None when any of the three env vars is missing — caller treats
    that as "parent RLS engine not configured" and routes accordingly.
    """
    _cfg = get_settings()
    user = _cfg.parent_db.user
    pw = _cfg.parent_db.password
    base = _cfg.core.database_url
    if not user or not pw or not base:
        return None
    parsed = urlparse(base)
    if not parsed.hostname:
        return None
    netloc = f"{user}:{pw}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def get_parent_engine() -> Engine | None:
    """Lazily-cached singleton engine for the RLS-enforced parent role.

    Returns None when env vars unset — Phase 0 default state. Phase 1+
    callers must check and either fail-loud (`get_parent_session_dep`) or
    fall through to the legacy engine. Never let "engine missing" silently
    revert to the BYPASSRLS admin path; that would bypass RLS for parents.
    """
    global _parent_engine_instance
    if _parent_engine_instance is not None:
        return _parent_engine_instance
    with _parent_engine_lock:
        if _parent_engine_instance is not None:
            return _parent_engine_instance
        url = _build_parent_url_from_env()
        if url is None:
            return None
        _parent_engine_instance = get_parent_engine_for_url(url)
        return _parent_engine_instance


def reset_parent_engine_for_tests() -> None:
    """Drop the cached engine so the next call re-reads env vars.

    Test-only. Tests that monkeypatch PARENT_DB_USER / PARENT_DB_PASSWORD must
    call this to invalidate any prior cache; production code paths should
    never invoke it.
    """
    global _parent_engine_instance
    with _parent_engine_lock:
        if _parent_engine_instance is not None:
            _parent_engine_instance.dispose()
            _parent_engine_instance = None


def get_parent_session_dep(user_id: int) -> Generator[Session, None, None]:
    """Generator-style FastAPI-compatible dep that yields a parent-scoped
    Session tied to `user_id`.

    Phase 1 router wraps this:

        from fastapi import Depends
        from models.parent_db import get_parent_session_dep
        from utils.auth import get_current_user

        def get_parent_db(current_user: dict = Depends(get_current_user)):
            yield from get_parent_session_dep(current_user["user_id"])

    Raises RuntimeError when parent engine not configured — fail-loud is
    intentional. Silent fallback to the admin (BYPASSRLS) engine would
    defeat the entire purpose of RLS.
    """
    engine = get_parent_engine()
    if engine is None:
        raise RuntimeError(
            "Parent RLS engine not configured. Set PARENT_DB_USER and "
            "PARENT_DB_PASSWORD in environment, ensure the parlsr001 migration "
            "has been applied, and ops has run `ALTER ROLE ivy_parent_login "
            "PASSWORD '...'` to set the password."
        )
    yield from build_parent_session_for_user(engine, user_id)
