"""4 critical wiring tests for parent RLS spike.

Validates the 3 claims from the design doc:
1. handler runs inside the SET LOCAL transaction (the §1.3 bug)
2. connection pool does NOT leak app.current_user_id across checkouts
3. admin engine (BYPASSRLS) sees all data, parent engine sees only its own

Run:
    cd ivy-backend
    pytest tests/spike_rls/ -v

Requires:
    Local PostgreSQL at localhost:5432/ivymanagement with superuser access.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models.parent_db import (
    build_parent_session_for_user,
    get_admin_engine_for_url,
    get_parent_engine_for_url,
)


def _query_attendance(session) -> list[tuple]:
    return [
        (row.student_id, row.note)
        for row in session.execute(
            text(
                "SELECT student_id, note FROM rls_spike.student_attendance "
                "ORDER BY id"
            )
        ).all()
    ]


# ---------------------------------------------------------------------------
# Test 1: handler-inside-SET-LOCAL (the advisor-caught bug)
# ---------------------------------------------------------------------------


def test_1_handler_query_sees_only_own_rows(spike_pg):
    """The yield must be INSIDE `with session.begin():`. If outside, SET LOCAL
    has already committed-and-died by the time the handler runs its SELECT,
    and the policy returns 0 rows for everything.

    This test catches that bug directly: if wiring is wrong, parent A would
    see 0 rows (not 2), and the assertion fails."""
    engine = get_parent_engine_for_url(spike_pg.parent_url)
    try:
        gen = build_parent_session_for_user(engine, spike_pg.parent_a_user_id)
        session = next(gen)

        rows = _query_attendance(session)

        student_ids = {sid for sid, _ in rows}
        assert student_ids == {spike_pg.student_a_id}, (
            f"parent A should see only their child (student {spike_pg.student_a_id}), "
            f"got student_ids={student_ids} rows={rows}"
        )
        assert len(rows) == 2, f"parent A has 2 attendance rows, got {len(rows)}"

        # Exhaust generator (triggers commit + close in the dep)
        for _ in gen:
            pass
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: pool isolation — SET LOCAL must not leak to next checkout
# ---------------------------------------------------------------------------


def test_2_pool_does_not_leak_user_id_across_checkouts(spike_pg):
    """Force a single-connection pool, then run two sequential requests with
    different parent user_ids. Confirms:
    (a) parent A's request sees only A's data
    (b) parent B's request sees only B's data (NOT A's)
    (c) a "no SET" raw query against the same pool sees 0 rows (fail-closed)
    """
    # Force pool of size 1 so the same physical connection is reused.
    engine = get_parent_engine_for_url(spike_pg.parent_url, pool_size=1, max_overflow=0)
    try:
        # Request 1 — parent A
        gen_a = build_parent_session_for_user(engine, spike_pg.parent_a_user_id)
        sess_a = next(gen_a)
        rows_a = _query_attendance(sess_a)
        for _ in gen_a:
            pass
        assert {sid for sid, _ in rows_a} == {spike_pg.student_a_id}

        # Request 2 — parent B reuses the same physical connection
        gen_b = build_parent_session_for_user(engine, spike_pg.parent_b_user_id)
        sess_b = next(gen_b)
        rows_b = _query_attendance(sess_b)
        for _ in gen_b:
            pass
        assert {sid for sid, _ in rows_b} == {
            spike_pg.student_b_id
        }, f"parent B request leaked into A's data: rows={rows_b}"

        # Request 3 — raw checkout WITHOUT calling our dep; app.current_user_id
        # should be empty (connect listener reset). Query must see 0 rows.
        SessionLocal = sessionmaker(bind=engine)
        raw_session = SessionLocal()
        try:
            raw_rows = _query_attendance(raw_session)
            assert raw_rows == [], (
                "Raw query without SET LOCAL must be fail-closed (0 rows); "
                f"got {raw_rows} — pool leak or listener missing."
            )
        finally:
            raw_session.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 3: admin engine bypasses RLS
# ---------------------------------------------------------------------------


def test_3_admin_engine_sees_all_rows(spike_pg):
    """Admin role has BYPASSRLS — should see all 4 attendance rows across
    both parents, regardless of app.current_user_id."""
    engine = get_admin_engine_for_url(spike_pg.admin_login_url)
    try:
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        try:
            rows = _query_attendance(session)
            student_ids = {sid for sid, _ in rows}
            assert student_ids == {
                spike_pg.student_a_id,
                spike_pg.student_b_id,
            }, f"admin should see all students, got {student_ids}"
            assert len(rows) == 4, f"admin should see all 4 rows, got {len(rows)}"
        finally:
            session.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 4: parent with no guardian sees 0 rows (not error, not 403)
# ---------------------------------------------------------------------------


def test_4_user_with_no_guardian_sees_zero_rows(spike_pg):
    """A logged-in user who isn't a parent of any student must see nothing.
    Validates the fail-closed shape of the policy (NULLIF + JOIN guardian)."""
    engine = get_parent_engine_for_url(spike_pg.parent_url)
    try:
        gen = build_parent_session_for_user(engine, spike_pg.no_child_user_id)
        session = next(gen)
        rows = _query_attendance(session)
        for _ in gen:
            pass
        assert (
            rows == []
        ), f"user_id={spike_pg.no_child_user_id} has no guardian; must see 0 rows, got {rows}"
    finally:
        engine.dispose()
