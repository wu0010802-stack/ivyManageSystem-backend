"""Phase 1 pilot: attendance.py via real RLS engine.

Validates the end-to-end FastAPI → get_parent_db → parent_engine → SET LOCAL →
RLS policy → row visibility chain using the migration-created ivy_parent_login
role against the real `public` schema. Seeded data lives in a high ID range
(99_001+) to avoid colliding with dev data; teardown removes it.

Skipped if:
- migration parlsr002 not applied (no policy on student_attendances)
- ivy_parent_login password not set to dev_parent_pw_2026_05_18

The base conftest.py monkey-patches JSONB→JSON for SQLite — those patches are
harmless here because attendance / guardian columns don't touch JSONB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from models import parent_db

_ADMIN_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"
_PARENT_LOGIN_PW = "dev_parent_pw_2026_05_18"

# High IDs to dodge dev seed data.
_USER_A = 99001
_USER_B = 99002
_USER_C = 99003  # no children
_STUDENT_A = 99001
_STUDENT_B = 99002
_DATES = [date(2026, 5, 1), date(2026, 5, 2)]
_YEAR = 2026
_MONTH = 5


def _phase1_ready() -> bool:
    """Skip helper: policy + role + password all wired?"""
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            policy_count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE tablename IN ('student_attendances','guardians')"
                )
            ).scalar()
            if policy_count < 2:
                admin_eng.dispose()
                return False
        admin_eng.dispose()
        parent_eng = create_engine(
            f"postgresql://ivy_parent_login:{_PARENT_LOGIN_PW}@localhost:5432/ivymanagement"
        )
        with parent_eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        parent_eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _phase1_ready(),
    reason="parlsr002 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1Context:
    admin_engine_dispose: callable


@pytest.fixture(scope="module")
def phase1_seed() -> Generator[Phase1Context, None, None]:
    """Seed 2 parents + 1 orphan + 2 students + 2 guardians + 4 attendance rows.

    Uses superuser via _ADMIN_URL — bypasses RLS for setup. Caller engine
    is disposed at teardown along with deleting all seeded rows by ID.
    """
    admin_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        # Clean any prior partial state
        _cleanup_rows(conn)

        # Users — schema has `display_name` (not full_name); role='parent'.
        for uid in (_USER_A, _USER_B, _USER_C):
            conn.execute(
                text("""
                INSERT INTO users (id, username, password_hash, role, display_name, is_active)
                VALUES (:id, :u, :pw, 'parent', :name, true)
                ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": uid,
                    "u": f"phase1_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1 Test {uid}",
                },
            )

        # Students — student_id (varchar20) NOT NULL; lifecycle_status defaults
        # to 'active'; classroom_id is nullable, use any existing for realism.
        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1-A", "Phase1 A"),
            (_STUDENT_B, "TEST-PHASE1-B", "Phase1 B"),
        ):
            conn.execute(
                text("""
                INSERT INTO students (id, student_id, name, classroom_id)
                VALUES (:id, :code, :n, :cid)
                ON CONFLICT (id) DO NOTHING
                """),
                {"id": sid, "code": code, "n": name, "cid": classroom_id},
            )

        # Guardians
        for uid, sid in ((_USER_A, _STUDENT_A), (_USER_B, _STUDENT_B)):
            conn.execute(
                text("""
                INSERT INTO guardians (user_id, student_id, name, is_primary, is_emergency, can_pickup, sort_order)
                VALUES (:u, :s, 'Test Guardian', true, false, true, 0)
                ON CONFLICT DO NOTHING
                """),
                {"u": uid, "s": sid},
            )

        # Attendance — 2 rows per student
        for sid in (_STUDENT_A, _STUDENT_B):
            for d in _DATES:
                conn.execute(
                    text("""
                    INSERT INTO student_attendances (student_id, date, status, remark)
                    VALUES (:s, :d, '出席', 'phase1-pilot-seed')
                    ON CONFLICT DO NOTHING
                    """),
                    {"s": sid, "d": d},
                )

    # Configure parent_db env vars. DATABASE_URL is required because
    # parent_db reads from os.environ directly (not via the dotenv-aware base
    # module), and the worktree has no .env file.
    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    parent_db.reset_parent_engine_for_tests()

    yield Phase1Context(admin_engine_dispose=admin_engine.dispose)

    # Teardown
    parent_db.reset_parent_engine_for_tests()
    os.environ.pop("PARENT_DB_USER", None)
    os.environ.pop("PARENT_DB_PASSWORD", None)
    cleanup_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with cleanup_engine.connect() as conn:
        _cleanup_rows(conn)
    cleanup_engine.dispose()
    admin_engine.dispose()


def _cleanup_rows(conn) -> None:
    conn.execute(
        text(
            "DELETE FROM student_attendances "
            "WHERE student_id IN (:a, :b) AND remark='phase1-pilot-seed'"
        ),
        {"a": _STUDENT_A, "b": _STUDENT_B},
    )
    conn.execute(
        text("DELETE FROM guardians WHERE user_id IN (:a, :b, :c)"),
        {"a": _USER_A, "b": _USER_B, "c": _USER_C},
    )
    conn.execute(
        text("DELETE FROM students WHERE id IN (:a, :b)"),
        {"a": _STUDENT_A, "b": _STUDENT_B},
    )
    conn.execute(
        text("DELETE FROM users WHERE id IN (:a, :b, :c)"),
        {"a": _USER_A, "b": _USER_B, "c": _USER_C},
    )


def _build_app(current_user_factory):
    """Build a minimal FastAPI app mounting the real attendance router,
    overriding get_current_user to return whatever the factory yields."""
    from api.parent_portal.attendance import router as attendance_router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(attendance_router, prefix="/api/parent")
    app.dependency_overrides[get_current_user] = current_user_factory
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parent_a_sees_own_child_attendance(phase1_seed):
    app = _build_app(lambda: {"user_id": _USER_A, "role": "parent"})
    with TestClient(app) as client:
        resp = client.get(
            f"/api/parent/attendance/monthly?student_id={_STUDENT_A}&year={_YEAR}&month={_MONTH}"
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recorded_days"] == 2, data
    assert {item["date"] for item in data["items"]} == {d.isoformat() for d in _DATES}


def test_parent_a_blocked_from_other_parents_child(phase1_seed):
    """First defense: _assert_student_owned returns 403 before any DB query."""
    app = _build_app(lambda: {"user_id": _USER_A, "role": "parent"})
    with TestClient(app) as client:
        resp = client.get(
            f"/api/parent/attendance/monthly?student_id={_STUDENT_B}&year={_YEAR}&month={_MONTH}"
        )
    assert resp.status_code == 403, resp.text


def test_user_with_no_children_blocked(phase1_seed):
    """User C has no guardian rows → _assert_student_owned 403 for any student."""
    app = _build_app(lambda: {"user_id": _USER_C, "role": "parent"})
    with TestClient(app) as client:
        resp = client.get(
            f"/api/parent/attendance/monthly?student_id={_STUDENT_A}&year={_YEAR}&month={_MONTH}"
        )
    assert resp.status_code == 403, resp.text


def test_rls_fail_closed_when_app_layer_skipped(phase1_seed):
    """Second defense: if a caller somehow bypasses _assert_student_owned and
    queries the engine directly with user_id=99003 (no children), RLS makes
    student_attendances appear empty. This is the hard-isolation guarantee."""
    engine = parent_db.get_parent_engine()
    assert engine is not None
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT student_id FROM student_attendances "
                "WHERE student_id IN (:a, :b)"
            ),
            {"a": _STUDENT_A, "b": _STUDENT_B},
        ).all()
        assert (
            rows == []
        ), f"user {_USER_C} has no guardian; RLS must hide attendance rows; got {rows}"
        # Also confirm guardians table is empty for this user
        own_rows = session.execute(
            text("SELECT id FROM guardians WHERE user_id = :u"),
            {"u": _USER_C},
        ).all()
        assert (
            own_rows == []
        ), f"guardians should be empty for user {_USER_C}: {own_rows}"
    finally:
        for _ in gen:
            pass


def test_existing_admin_engine_unaffected_by_rls(phase1_seed):
    """Existing admin path (DATABASE_URL credentials) must continue to see
    all rows after phase 1 RLS goes live. This is the "no behavior change
    for non-parent_portal routers" invariant.

    Note: we don't test `ivy_admin_login` here because Phase 0 didn't GRANT
    any tables to it — that role exists for a future hardening where the
    main DATABASE_URL switches to it. For now the existing yilunwu/superuser
    credentials own the tables and bypass RLS by ownership."""
    with admin_engine_dispose() as admin_eng:
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM student_attendances "
                    "WHERE student_id IN (:a, :b)"
                ),
                {"a": _STUDENT_A, "b": _STUDENT_B},
            ).scalar()
            assert count == 4, f"admin should see all 4 seeded rows, got {count}"


from contextlib import contextmanager


@contextmanager
def admin_engine_dispose():
    eng = create_engine(_ADMIN_URL)
    try:
        yield eng
    finally:
        eng.dispose()
