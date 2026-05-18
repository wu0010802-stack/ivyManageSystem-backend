"""Phase 1b: leaves + attachments + polymorphic policy isolation against real PG.

Validates the migration parlsr003 deliverables:
- student_leave_requests Class A policy isolates per parent
- attachments Class D policy via `parent_owns_attachment` handles
  `owner_type='student_leave'` correctly; unknown owner_type → 0 row
- holidays / workday_overrides remain readable (no RLS) to parent role
- WITH CHECK on student_leave_requests rejects forging an INSERT with another
  parent's student_id

Skipped if migration parlsr003 / ivy_parent_login password not present.
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

# Disjoint from Phase 1 seed IDs (99001+) to avoid collisions.
_USER_A = 99101
_USER_B = 99102
_USER_C = 99103  # no children
_STUDENT_A = 99101
_STUDENT_B = 99102


def _phase1b_ready() -> bool:
    """Both phase 1 + phase 1b policies in place + ivy_parent_login reachable?"""
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            # Need 4 policies total (phase 1's 2 + phase 1b's 2)
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE tablename IN ('student_attendances','guardians','student_leave_requests','attachments')"
                )
            ).scalar()
            fn_count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname='parent_owns_attachment'"
                )
            ).scalar()
            admin_eng.dispose()
            if count < 4 or fn_count < 1:
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
    not _phase1b_ready(),
    reason="parlsr003 not applied OR parent_owns_attachment missing OR "
    "ivy_parent_login password mismatch",
)


@dataclass
class Phase1bContext:
    leave_a_id: int
    leave_b_id: int


@pytest.fixture(scope="module")
def phase1b_seed() -> Generator[Phase1bContext, None, None]:
    """Seed 2 parents + 2 students + 2 guardians + 2 leave_requests + 2 attachments."""
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
                    "u": f"phase1b_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1b Test {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1B-A", "Phase1b A"),
            (_STUDENT_B, "TEST-PHASE1B-B", "Phase1b B"),
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

        # Leave requests
        leave_ids = {}
        for sid, uid, key in (
            (_STUDENT_A, _USER_A, "A"),
            (_STUDENT_B, _USER_B, "B"),
        ):
            res = conn.execute(
                text("""
                INSERT INTO student_leave_requests
                  (student_id, applicant_user_id, leave_type, start_date, end_date, status, reason)
                VALUES
                  (:s, :u, 'sick', :sd, :ed, 'approved', 'phase1b-seed')
                RETURNING id
                """),
                {
                    "s": sid,
                    "u": uid,
                    "sd": date(2026, 5, 20),
                    "ed": date(2026, 5, 21),
                },
            )
            leave_ids[key] = res.scalar()

        # One attachment per leave (owner_type='student_leave')
        for key, leave_id in leave_ids.items():
            conn.execute(
                text("""
                INSERT INTO attachments
                  (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
                VALUES
                  ('student_leave', :lid, :sk, :fn, 'image/jpeg', 1024)
                """),
                {
                    "lid": leave_id,
                    "sk": f"phase1b-seed-{key}.jpg",
                    "fn": f"phase1b-seed-{key}.jpg",
                },
            )

    # Configure parent_db env
    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    parent_db.reset_parent_engine_for_tests()

    yield Phase1bContext(leave_a_id=leave_ids["A"], leave_b_id=leave_ids["B"])

    parent_db.reset_parent_engine_for_tests()
    os.environ.pop("PARENT_DB_USER", None)
    os.environ.pop("PARENT_DB_PASSWORD", None)
    cleanup_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with cleanup_engine.connect() as conn:
        _cleanup(conn)
    cleanup_engine.dispose()
    admin_engine.dispose()


def _cleanup(conn) -> None:
    conn.execute(
        text(
            "DELETE FROM attachments "
            "WHERE owner_type='student_leave' AND original_filename LIKE 'phase1b-seed%'"
        )
    )
    conn.execute(text("DELETE FROM student_leave_requests WHERE reason='phase1b-seed'"))
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


def test_parent_a_sees_only_own_leave_requests(phase1b_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, student_id FROM student_leave_requests "
                "WHERE id IN (:a, :b)"
            ),
            {"a": phase1b_seed.leave_a_id, "b": phase1b_seed.leave_b_id},
        ).all()
        assert {r.id for r in rows} == {
            phase1b_seed.leave_a_id
        }, f"parent A must see only own leave; got {rows}"
    finally:
        for _ in gen:
            pass


def test_parent_a_cannot_forge_insert_for_other_parents_student(phase1b_seed):
    """WITH CHECK on student_leave_requests must reject INSERT where the
    student_id isn't in this parent's guardians list."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                INSERT INTO student_leave_requests
                  (student_id, applicant_user_id, leave_type, start_date, end_date, status, reason)
                VALUES (:s, :u, 'sick', :sd, :ed, 'approved', 'phase1b-forge-test')
                """),
                {
                    "s": _STUDENT_B,  # parent A trying to insert for student B
                    "u": _USER_A,
                    "sd": date(2026, 5, 22),
                    "ed": date(2026, 5, 22),
                },
            )
        # Postgres reports "new row violates row-level security policy"
        assert "row-level security policy" in str(exc.value).lower(), exc.value
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        for _ in gen:
            pass


def test_attachment_policy_recognises_student_leave_owner(phase1b_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT owner_type, owner_id, original_filename FROM attachments "
                "WHERE original_filename LIKE 'phase1b-seed%'"
            )
        ).all()
        assert len(rows) == 1, f"parent A should see only own attachment; got {rows}"
        assert rows[0].owner_id == phase1b_seed.leave_a_id
    finally:
        for _ in gen:
            pass


def test_attachment_policy_fails_closed_for_unknown_owner_type(phase1b_seed):
    """parent_owns_attachment for unknown owner_type returns false → policy
    hides all such rows from parent.

    We INSERT (as admin/owner who bypasses RLS) a row with an unrecognised
    owner_type, then query as parent — must be invisible."""
    admin = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text("""
            INSERT INTO attachments
              (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
            VALUES
              ('made_up_owner_type', :lid, 'phase1b-unknown.bin',
               'phase1b-seed-unknown.bin', 'application/octet-stream', 1)
            """),
            {"lid": phase1b_seed.leave_a_id},
        )
    admin.dispose()

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, owner_type FROM attachments "
                "WHERE original_filename = 'phase1b-seed-unknown.bin'"
            )
        ).all()
        assert (
            rows == []
        ), f"Unknown owner_type must fail-closed under policy; saw {rows}"
    finally:
        for _ in gen:
            pass


def test_neutral_tables_readable_by_parent(phase1b_seed):
    """holidays + workday_overrides have no RLS but were GRANT'd SELECT to
    ivy_parent_role — parent must be able to read them for leave date validation."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        # Don't care about contents, just that we can SELECT without
        # permission-denied.
        session.execute(
            text("SELECT count(*) FROM holidays WHERE date > '1900-01-01'")
        ).scalar()
        session.execute(
            text("SELECT count(*) FROM workday_overrides WHERE date > '1900-01-01'")
        ).scalar()
    finally:
        for _ in gen:
            pass


def test_user_with_no_guardian_sees_no_leaves_or_attachments(phase1b_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        leave_rows = session.execute(
            text("SELECT id FROM student_leave_requests " "WHERE id IN (:a, :b)"),
            {"a": phase1b_seed.leave_a_id, "b": phase1b_seed.leave_b_id},
        ).all()
        assert leave_rows == [], leave_rows

        att_rows = session.execute(
            text(
                "SELECT id FROM attachments WHERE original_filename LIKE 'phase1b-seed%'"
            )
        ).all()
        assert att_rows == [], att_rows
    finally:
        for _ in gen:
            pass
