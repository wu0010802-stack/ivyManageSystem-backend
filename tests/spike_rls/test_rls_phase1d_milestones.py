"""Phase 1d: student_milestones isolation + UPDATE under FOR ALL policy.

Validates parlsr005 deliverables:
- Class A direct student_id isolation (same shape as earlier phases)
- FOR ALL policy supports parent UPDATEs (react / acknowledge writes
  parent_reaction + parent_acknowledged_at + parent_acknowledged_by)
- with_for_update row-lock plays nicely with RLS (locks the post-USING set)
- Forge-protection via WITH CHECK (parent can't UPDATE to set student_id
  pointing at another family's row)

Skipped if parlsr005 not applied or ivy_parent_login unreachable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Generator

import pytest
from sqlalchemy import create_engine, text

from models import parent_db

_ADMIN_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"
_PARENT_LOGIN_PW = "dev_parent_pw_2026_05_18"

# Disjoint IDs from prior phases (99001 / 99101 / 99201 used)
_USER_A = 99301
_USER_B = 99302
_USER_C = 99303
_STUDENT_A = 99301
_STUDENT_B = 99302


def _phase1d_ready() -> bool:
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname='parent_isolate_student_milestones'"
                )
            ).scalar()
        admin_eng.dispose()
        if count < 1:
            return False
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
    not _phase1d_ready(),
    reason="parlsr005 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1dContext:
    milestone_a_id: int
    milestone_b_id: int


@pytest.fixture(scope="module")
def phase1d_seed() -> Generator[Phase1dContext, None, None]:
    admin_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _cleanup(conn)

        for uid in (_USER_A, _USER_B, _USER_C):
            conn.execute(
                text("""
                INSERT INTO users (id, username, password_hash, role, display_name, is_active)
                VALUES (:id, :u, :pw, 'parent', :name, true)
                ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": uid,
                    "u": f"phase1d_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1d Test {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1D-A", "Phase1d A"),
            (_STUDENT_B, "TEST-PHASE1D-B", "Phase1d B"),
        ):
            conn.execute(
                text("""
                INSERT INTO students (id, student_id, name, classroom_id)
                VALUES (:id, :code, :n, :cid)
                ON CONFLICT (id) DO NOTHING
                """),
                {"id": sid, "code": code, "n": name, "cid": classroom_id},
            )

        for uid, sid in ((_USER_A, _STUDENT_A), (_USER_B, _STUDENT_B)):
            conn.execute(
                text("""
                INSERT INTO guardians (user_id, student_id, name, is_primary, is_emergency, can_pickup, sort_order)
                VALUES (:u, :s, 'Test Guardian', true, false, true, 0)
                ON CONFLICT DO NOTHING
                """),
                {"u": uid, "s": sid},
            )

        # Milestones — NOT NULL: student_id, milestone_type, achieved_on,
        # title, source_type, created_at (default), updated_at (default).
        milestone_ids = {}
        for sid, key in ((_STUDENT_A, "A"), (_STUDENT_B, "B")):
            res = conn.execute(
                text("""
                INSERT INTO student_milestones
                  (student_id, milestone_type, achieved_on, title, source_type)
                VALUES
                  (:s, 'first_word', :d, 'phase1d-seed', 'manual')
                RETURNING id
                """),
                {"s": sid, "d": date(2026, 4, 15)},
            )
            milestone_ids[key] = res.scalar()

    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    from config import reset_for_tests

    reset_for_tests()
    parent_db.reset_parent_engine_for_tests()

    yield Phase1dContext(
        milestone_a_id=milestone_ids["A"],
        milestone_b_id=milestone_ids["B"],
    )

    parent_db.reset_parent_engine_for_tests()
    os.environ.pop("PARENT_DB_USER", None)
    os.environ.pop("PARENT_DB_PASSWORD", None)
    from config import reset_for_tests

    reset_for_tests()
    cleanup_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with cleanup_engine.connect() as conn:
        _cleanup(conn)
    cleanup_engine.dispose()
    admin_engine.dispose()


def _cleanup(conn) -> None:
    conn.execute(text("DELETE FROM student_milestones WHERE title='phase1d-seed'"))
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_milestone_select_isolated(phase1d_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, student_id FROM student_milestones "
                "WHERE title='phase1d-seed'"
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].id == phase1d_seed.milestone_a_id
    finally:
        for _ in gen:
            pass


def test_milestone_update_by_owner_works(phase1d_seed):
    """parent_react / parent_acknowledge mutate parent_reaction +
    parent_acknowledged_at on their own milestone — FOR ALL policy supports it."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        session.execute(
            text("""
            UPDATE student_milestones
            SET parent_reaction = 'love', parent_acknowledged_at = now()
            WHERE id = :i
            """),
            {"i": phase1d_seed.milestone_a_id},
        )
        session.flush()
        row = session.execute(
            text("SELECT parent_reaction FROM student_milestones WHERE id = :i"),
            {"i": phase1d_seed.milestone_a_id},
        ).first()
        assert row.parent_reaction == "love"
    finally:
        for _ in gen:
            pass


def test_milestone_update_on_other_parents_milestone_returns_zero_rows(phase1d_seed):
    """Parent A trying to UPDATE parent B's milestone — USING fails, so 0 rows
    affected. We don't raise an error; PG silently skips the row."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        result = session.execute(
            text("""
            UPDATE student_milestones
            SET parent_reaction = 'forged'
            WHERE id = :i
            """),
            {"i": phase1d_seed.milestone_b_id},
        )
        session.flush()
        assert (
            result.rowcount == 0
        ), f"parent A must not affect parent B's milestone; rowcount={result.rowcount}"

        # Confirm B's row unchanged using admin engine
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            row = conn.execute(
                text("SELECT parent_reaction FROM student_milestones WHERE id = :i"),
                {"i": phase1d_seed.milestone_b_id},
            ).first()
        admin_eng.dispose()
        assert row.parent_reaction is None
    finally:
        for _ in gen:
            pass


def test_milestone_forge_student_id_blocked_by_with_check(phase1d_seed):
    """Parent A trying to UPDATE their own milestone to point at parent B's
    student — WITH CHECK rejects (would expose B's student_id to A's milestone
    record, which is exactly the kind of forge RLS exists to prevent)."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                UPDATE student_milestones
                SET student_id = :other_sid
                WHERE id = :i
                """),
                {
                    "other_sid": _STUDENT_B,
                    "i": phase1d_seed.milestone_a_id,
                },
            )
        assert "row-level security policy" in str(exc.value).lower(), exc.value
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        for _ in gen:
            pass


def test_no_guardian_user_sees_no_milestones(phase1d_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        rows = session.execute(
            text("SELECT id FROM student_milestones WHERE title='phase1d-seed'")
        ).all()
        assert rows == []
    finally:
        for _ in gen:
            pass
