"""Shared helpers for parent_portal RLS tests.

The parent dep `get_parent_db` wraps the session in PG's `with session.begin():`
which auto-commits on success. SQLite-backed tests can't get that for free —
they need an override that mimics the same lifecycle (commit on success,
rollback on error, always close).

Use this helper in any test that uses TestClient + SQLite + a router that
has been migrated to `Depends(get_parent_db)`. See test_parent_attendance_idor
or test_parent_student_leave for usage examples.
"""

from __future__ import annotations

from typing import Callable, Generator

from sqlalchemy import Engine, event
from sqlalchemy.orm import Session


def register_sqlite_parent_rls_udfs(engine: Engine) -> None:
    """Register SQLite UDFs that mirror Postgres-only helpers used by RLS-
    migrated parent_portal routers.

    Currently registers:
    - `public_count_enrolled(course_id)` — Phase 1f activity helper. In Postgres
      this is a `SECURITY DEFINER` plpgsql function that bypasses RLS to count
      across all parents (so catalog UI shows correct is_full). In SQLite tests
      we just run the query inline as a Python UDF — RLS doesn't exist here.

    Call this once when building the SQLite engine, BEFORE TestClient runs any
    requests. See test_parent_activity.py for usage.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        def _public_count_enrolled(course_id):
            cur = dbapi_conn.cursor()
            try:
                cur.execute(
                    "SELECT count(*) FROM registration_courses "
                    "WHERE course_id = ? "
                    "AND status IN ('enrolled', 'promoted_pending')",
                    (course_id,),
                )
                row = cur.fetchone()
                return row[0] if row else 0
            finally:
                cur.close()

        dbapi_conn.create_function("public_count_enrolled", 1, _public_count_enrolled)

    # Force the pool to drop any cached connections so the listener fires on
    # the next checkout. Without this, fixtures that called create_all (or any
    # other query) before this function would reuse a pre-listener connection
    # and the UDF wouldn't be registered on it.
    engine.dispose()


def make_sqlite_parent_db_override(
    session_factory: Callable[[], Session],
) -> Callable[[], Generator[Session, None, None]]:
    """Return a FastAPI dep override that yields a SQLite session with
    commit-on-success / rollback-on-error / always-close semantics.

    Use in tests:

        from tests._parent_rls_test_utils import make_sqlite_parent_db_override
        from api.parent_portal._dependencies import get_parent_db

        app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
            session_factory
        )
    """

    def _override() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _override
