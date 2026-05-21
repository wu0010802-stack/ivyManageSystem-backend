"""Phase 1f: activity router + students retro-RLS isolation.

Validates parlsr007 deliverables:
- students table newly RLS-enabled (retroactive fix for phases 1b/1e
  for_write=True flows that read Student.lifecycle_status)
- activity_registrations + 3 subresources (courses/supplies/payments) +
  registration_changes audit log isolated per parent
- activity_courses + activity_supplies catalogs remain readable (no RLS,
  parent sees all available courses)
- public_count_enrolled() SECURITY DEFINER bypasses RLS for catalog UI
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Generator

import pytest
from sqlalchemy import create_engine, text

from models import parent_db

_ADMIN_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"
_PARENT_LOGIN_PW = "dev_parent_pw_2026_05_18"

_USER_A = 99501
_USER_B = 99502
_USER_C = 99503
_STUDENT_A = 99501
_STUDENT_B = 99502


def _phase1f_ready() -> bool:
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname IN ("
                    "'parent_isolate_students',"
                    "'parent_isolate_activity_registrations',"
                    "'parent_isolate_registration_courses',"
                    "'parent_isolate_registration_supplies',"
                    "'parent_isolate_activity_payment_records',"
                    "'parent_isolate_registration_changes')"
                )
            ).scalar()
            fn_count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname='public_count_enrolled'"
                )
            ).scalar()
        admin_eng.dispose()
        if count < 6 or fn_count < 1:
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
    not _phase1f_ready(),
    reason="parlsr007 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1fContext:
    course_a_id: int  # public catalog
    course_b_id: int  # public catalog
    reg_a_id: int
    reg_b_id: int
    rc_a_id: int  # registration course (parent A)


@pytest.fixture(scope="module")
def phase1f_seed() -> Generator[Phase1fContext, None, None]:
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
                    "u": f"phase1f_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1f {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1F-A", "Phase1f A"),
            (_STUDENT_B, "TEST-PHASE1F-B", "Phase1f B"),
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

        # Public catalog: two courses
        course_ids = {}
        for key, name in (("A", "phase1f-seed-courseA"), ("B", "phase1f-seed-courseB")):
            res = conn.execute(
                text("""
                INSERT INTO activity_courses
                  (name, price, school_year, semester, capacity, is_active, allow_waitlist)
                VALUES (:n, 1000, 113, 1, 30, true, true)
                RETURNING id
                """),
                {"n": name},
            )
            course_ids[key] = res.scalar()

        # Two registrations
        reg_ids = {}
        for sid, key in ((_STUDENT_A, "A"), (_STUDENT_B, "B")):
            res = conn.execute(
                text("""
                INSERT INTO activity_registrations
                  (student_id, student_name, school_year, semester, paid_amount,
                   pending_review, match_status, is_active)
                VALUES (:s, :sn, 113, 1, 0, false, 'manual', true)
                RETURNING id
                """),
                {"s": sid, "sn": f"Phase1f {sid}"},
            )
            reg_ids[key] = res.scalar()

        # Parent A registered for course A
        rc_res = conn.execute(
            text("""
            INSERT INTO registration_courses
              (registration_id, course_id, status)
            VALUES (:r, :c, 'enrolled')
            RETURNING id
            """),
            {"r": reg_ids["A"], "c": course_ids["A"]},
        )
        rc_a_id = rc_res.scalar()

        # Audit log entry for parent A
        conn.execute(
            text("""
            INSERT INTO registration_changes
              (registration_id, student_name, change_type, description, changed_by)
            VALUES (:r, :sn, 'phase1f-seed', 'seed entry', 'parent')
            """),
            {"r": reg_ids["A"], "sn": f"Phase1f {_STUDENT_A}"},
        )

        # Payment for parent A
        conn.execute(
            text("""
            INSERT INTO activity_payment_records
              (registration_id, type, amount, payment_date, payment_method)
            VALUES (:r, 'tuition', 1000, '2026-05-01', 'cash')
            """),
            {"r": reg_ids["A"]},
        )

    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    from config import reset_for_tests

    reset_for_tests()
    parent_db.reset_parent_engine_for_tests()

    yield Phase1fContext(
        course_a_id=course_ids["A"],
        course_b_id=course_ids["B"],
        reg_a_id=reg_ids["A"],
        reg_b_id=reg_ids["B"],
        rc_a_id=rc_a_id,
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
    conn.execute(
        text(
            "DELETE FROM activity_payment_records "
            "WHERE registration_id IN (SELECT id FROM activity_registrations "
            "WHERE student_name LIKE 'Phase1f%')"
        )
    )
    conn.execute(
        text(
            "DELETE FROM registration_changes "
            "WHERE student_name LIKE 'Phase1f%' OR change_type='phase1f-seed'"
        )
    )
    conn.execute(
        text(
            "DELETE FROM registration_courses "
            "WHERE registration_id IN (SELECT id FROM activity_registrations "
            "WHERE student_name LIKE 'Phase1f%')"
        )
    )
    conn.execute(
        text(
            "DELETE FROM registration_supplies "
            "WHERE registration_id IN (SELECT id FROM activity_registrations "
            "WHERE student_name LIKE 'Phase1f%')"
        )
    )
    conn.execute(
        text("DELETE FROM activity_registrations WHERE student_name LIKE 'Phase1f%'")
    )
    conn.execute(text("DELETE FROM activity_courses WHERE name LIKE 'phase1f-seed%'"))
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


def test_students_retro_fix_parent_sees_own_kid(phase1f_seed):
    """Retroactive students RLS: parent A reads own student row OK."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text("SELECT id, lifecycle_status FROM students " "WHERE id IN (:a, :b)"),
            {"a": _STUDENT_A, "b": _STUDENT_B},
        ).all()
        ids = {r.id for r in rows}
        assert ids == {_STUDENT_A}, f"parent A must only see own student; got {ids}"
        assert rows[0].lifecycle_status is not None  # column readable
    finally:
        for _ in gen:
            pass


def test_students_retro_fix_for_write_path_works(phase1f_seed):
    """The original latent bug: leaves.py / medications.py POST flows call
    `_assert_student_owned(for_write=True)` which reads `students.lifecycle_status`.
    Before parlsr007 this 500'd with permission denied. Verify the GRANT +
    policy combo makes the query succeed for own kid."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        row = session.execute(
            text("SELECT lifecycle_status FROM students WHERE id = :i"),
            {"i": _STUDENT_A},
        ).first()
        assert row is not None
    finally:
        for _ in gen:
            pass


def test_activity_registrations_isolated(phase1f_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id FROM activity_registrations "
                "WHERE student_name LIKE 'Phase1f%'"
            )
        ).all()
        assert {r.id for r in rows} == {phase1f_seed.reg_a_id}
    finally:
        for _ in gen:
            pass


def test_registration_subresources_via_join(phase1f_seed):
    """Parent B should NOT see parent A's registration_courses / payments /
    audit changes — all 3 use registration_id JOIN policy."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        # registration_courses for A's reg
        rcs = session.execute(
            text("SELECT id FROM registration_courses WHERE registration_id = :r"),
            {"r": phase1f_seed.reg_a_id},
        ).all()
        assert rcs == []

        # payments
        payments = session.execute(
            text(
                "SELECT id FROM activity_payment_records " "WHERE registration_id = :r"
            ),
            {"r": phase1f_seed.reg_a_id},
        ).all()
        assert payments == []

        # audit changes
        changes = session.execute(
            text("SELECT id FROM registration_changes WHERE registration_id = :r"),
            {"r": phase1f_seed.reg_a_id},
        ).all()
        assert changes == []
    finally:
        for _ in gen:
            pass


def test_public_catalog_visible_to_all_parents(phase1f_seed):
    """activity_courses + activity_supplies catalogs have GRANT SELECT but no
    RLS — every parent sees the full catalog (this is intentional)."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, name FROM activity_courses WHERE name LIKE 'phase1f-seed%' ORDER BY name"
            )
        ).all()
        # Parent B sees BOTH courses (A and B) even though only registered for none
        assert (
            len(rows) == 2
        ), f"parent B should see both public catalog courses; got {rows}"
    finally:
        for _ in gen:
            pass


def test_public_count_enrolled_bypasses_rls(phase1f_seed):
    """SECURITY DEFINER function returns the true count across all parents,
    not just the calling parent's. Parent B has 0 registrations on courseA
    but the count should reflect parent A's 1 enrollment."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        cnt = session.execute(
            text("SELECT public_count_enrolled(:c)"),
            {"c": phase1f_seed.course_a_id},
        ).scalar()
        assert (
            cnt == 1
        ), f"public_count_enrolled must return 1 (A's enrollment), got {cnt}"

        # Parent B's own RLS-scoped query returns 0 since they're not enrolled
        own_cnt = session.execute(
            text("SELECT count(*) FROM registration_courses WHERE course_id = :c"),
            {"c": phase1f_seed.course_a_id},
        ).scalar()
        assert own_cnt == 0, f"parent B's RLS-scoped count must be 0, got {own_cnt}"
    finally:
        for _ in gen:
            pass


def test_with_check_blocks_forge_registration_insert(phase1f_seed):
    """Parent A trying to insert an activity_registration for parent B's
    student is blocked by WITH CHECK."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                INSERT INTO activity_registrations
                  (student_id, student_name, school_year, semester, paid_amount,
                   pending_review, match_status, is_active)
                VALUES (:s, 'forge-attempt', 113, 1, 0, false, 'manual', true)
                """),
                {"s": _STUDENT_B},
            )
        assert "row-level security policy" in str(exc.value).lower(), exc.value
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        for _ in gen:
            pass


def test_no_guardian_user_sees_nothing(phase1f_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        # Should see no own student
        students = session.execute(
            text("SELECT id FROM students WHERE id IN (:a, :b)"),
            {"a": _STUDENT_A, "b": _STUDENT_B},
        ).all()
        assert students == []

        # No registrations
        regs = session.execute(
            text(
                "SELECT id FROM activity_registrations WHERE student_name LIKE 'Phase1f%'"
            )
        ).all()
        assert regs == []
    finally:
        for _ in gen:
            pass
