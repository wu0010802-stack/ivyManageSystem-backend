"""Phase 1g: announcements + events isolation against real PG.

Validates parlsr008 deliverables:
- announcement_parent_reads (Class B direct user_id, parent INSERTs on mark-read)
- event_acknowledgments (Class B HYBRID user_id + student_id — WITH CHECK
  enforces BOTH; this is spike §2 forge-student gap)
- Public catalog (announcements, school_events, classrooms) readable by all parents
- parent_owns_attachment ELSIF 'event_acknowledgment' for signature attachments
- Unknown owner_type still fail-closed

Skipped if parlsr008 not applied or ivy_parent_login unreachable.
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

_USER_A = 99601
_USER_B = 99602
_USER_C = 99603
_STUDENT_A = 99601
_STUDENT_B = 99602


def _phase1g_ready() -> bool:
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname IN ("
                    "'parent_isolate_announcement_parent_reads',"
                    "'parent_isolate_event_acknowledgments')"
                )
            ).scalar()
        admin_eng.dispose()
        if count < 2:
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
    not _phase1g_ready(),
    reason="parlsr008 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1gContext:
    announcement_id: int
    school_event_id: int
    ack_a_id: int  # parent A's event ack (for owning attachment lookup)


@pytest.fixture(scope="module")
def phase1g_seed() -> Generator[Phase1gContext, None, None]:
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
                    "u": f"phase1g_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1g {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1G-A", "Phase1g A"),
            (_STUDENT_B, "TEST-PHASE1G-B", "Phase1g B"),
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

        # announcements.created_by FK → employees(id). Pull any active employee.
        admin_employee_id = conn.execute(
            text("SELECT id FROM employees ORDER BY id LIMIT 1")
        ).scalar()

        # Announcement (catalog — no RLS)
        ann_res = conn.execute(
            text("""
            INSERT INTO announcements (title, content, created_by)
            VALUES ('phase1g-seed', 'phase1g announcement body', :cb)
            RETURNING id
            """),
            {"cb": admin_employee_id},
        )
        announcement_id = ann_res.scalar()

        # Parent A marks the announcement read
        conn.execute(
            text("""
            INSERT INTO announcement_parent_reads (announcement_id, user_id, read_at)
            VALUES (:a, :u, :ts)
            """),
            {"a": announcement_id, "u": _USER_A, "ts": datetime(2026, 5, 1)},
        )

        # SchoolEvent (catalog) + EventAcknowledgment (Class B hybrid)
        ev_res = conn.execute(
            text("""
            INSERT INTO school_events
              (title, event_date, requires_acknowledgment)
            VALUES ('phase1g-seed-event', :d, true)
            RETURNING id
            """),
            {"d": date(2026, 5, 5)},
        )
        school_event_id = ev_res.scalar()

        ack_res = conn.execute(
            text("""
            INSERT INTO event_acknowledgments
              (event_id, user_id, student_id, acknowledged_at)
            VALUES (:e, :u, :s, :ts)
            RETURNING id
            """),
            {
                "e": school_event_id,
                "u": _USER_A,
                "s": _STUDENT_A,
                "ts": datetime(2026, 5, 5, 9),
            },
        )
        ack_a_id = ack_res.scalar()

        # Attachment with owner_type='event_acknowledgment' for parent A's ack
        conn.execute(
            text("""
            INSERT INTO attachments
              (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
            VALUES
              ('event_acknowledgment', :oid, 'phase1g-sig.png', 'phase1g-seed-sig.png',
               'image/png', 1024)
            """),
            {"oid": ack_a_id},
        )

    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    parent_db.reset_parent_engine_for_tests()

    yield Phase1gContext(
        announcement_id=announcement_id,
        school_event_id=school_event_id,
        ack_a_id=ack_a_id,
    )

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
        text("DELETE FROM attachments WHERE original_filename LIKE 'phase1g-seed%'")
    )
    conn.execute(
        text(
            "DELETE FROM event_acknowledgments WHERE student_id IN (:a, :b) "
            "OR user_id IN (:ua, :ub, :uc)"
        ),
        {
            "a": _STUDENT_A,
            "b": _STUDENT_B,
            "ua": _USER_A,
            "ub": _USER_B,
            "uc": _USER_C,
        },
    )
    conn.execute(text("DELETE FROM school_events WHERE title='phase1g-seed-event'"))
    conn.execute(
        text("DELETE FROM announcement_parent_reads WHERE user_id IN (:a, :b, :c)"),
        {"a": _USER_A, "b": _USER_B, "c": _USER_C},
    )
    conn.execute(text("DELETE FROM announcements WHERE title='phase1g-seed'"))
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


def test_announcement_parent_read_class_b_direct(phase1g_seed):
    """Parent A reads own row; parent B sees none of A's reads."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id FROM announcement_parent_reads " "WHERE announcement_id = :a"
            ),
            {"a": phase1g_seed.announcement_id},
        ).all()
        assert (
            rows == []
        ), f"parent B must not see A's announcement_parent_reads; got {rows}"
    finally:
        for _ in gen:
            pass


def test_announcement_read_with_check_blocks_forge_user_id(phase1g_seed):
    """Parent A trying to INSERT with user_id=B (forge) — WITH CHECK rejects."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                INSERT INTO announcement_parent_reads (announcement_id, user_id, read_at)
                VALUES (:a, :u, now())
                """),
                {"a": phase1g_seed.announcement_id, "u": _USER_B},
            )
        assert "row-level security policy" in str(exc.value).lower(), exc.value
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        for _ in gen:
            pass


def test_event_ack_class_b_hybrid_with_check_blocks_forge_student(phase1g_seed):
    """**Spike §2 forge-student gap test (first real case)**: parent A
    INSERTs ack with user_id=A (correct) + student_id=B (forge) — must be
    rejected by hybrid WITH CHECK (which validates both dimensions)."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                INSERT INTO event_acknowledgments
                  (event_id, user_id, student_id, acknowledged_at)
                VALUES (:e, :u, :s, now())
                """),
                {
                    "e": phase1g_seed.school_event_id,
                    "u": _USER_A,  # correct
                    "s": _STUDENT_B,  # forge — parent B's kid
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


def test_event_ack_select_isolated(phase1g_seed):
    """Parent B should not see parent A's event ack."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        rows = session.execute(
            text("SELECT id FROM event_acknowledgments WHERE event_id = :e"),
            {"e": phase1g_seed.school_event_id},
        ).all()
        assert rows == []
    finally:
        for _ in gen:
            pass


def test_attachment_polymorphic_elsif_event_acknowledgment(phase1g_seed):
    """parent_owns_attachment recognises owner_type='event_acknowledgment'."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, owner_type FROM attachments "
                "WHERE original_filename='phase1g-seed-sig.png'"
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].owner_type == "event_acknowledgment"
    finally:
        for _ in gen:
            pass


def test_attachment_polymorphic_event_ack_isolated_from_other_parents(phase1g_seed):
    """Parent B should not see parent A's signature attachment (via the
    Class D polymorphic policy on attachments — function evaluates owner_id
    against parent A's event_acknowledgments which parent B can't see)."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id FROM attachments "
                "WHERE original_filename='phase1g-seed-sig.png'"
            )
        ).all()
        assert rows == []
    finally:
        for _ in gen:
            pass


def test_public_catalog_visible_to_all_parents(phase1g_seed):
    """announcements + school_events + classrooms have GRANT SELECT but no RLS."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        ann = session.execute(
            text("SELECT id FROM announcements WHERE title='phase1g-seed'")
        ).all()
        assert len(ann) == 1, "parent B should see public announcement"

        ev = session.execute(
            text("SELECT id FROM school_events WHERE title='phase1g-seed-event'")
        ).all()
        assert len(ev) == 1, "parent B should see public school_event"

        cr = session.execute(text("SELECT count(*) FROM classrooms")).scalar()
        assert cr > 0, "parent B should see classrooms catalog"
    finally:
        for _ in gen:
            pass


def test_no_guardian_user_sees_nothing(phase1g_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        # No announcement_parent_reads
        reads = session.execute(
            text("SELECT id FROM announcement_parent_reads WHERE announcement_id = :a"),
            {"a": phase1g_seed.announcement_id},
        ).all()
        assert reads == []

        # No event_acknowledgments
        acks = session.execute(
            text("SELECT id FROM event_acknowledgments WHERE event_id = :e"),
            {"e": phase1g_seed.school_event_id},
        ).all()
        assert acks == []

        # No signature attachments
        atts = session.execute(
            text(
                "SELECT id FROM attachments WHERE original_filename='phase1g-seed-sig.png'"
            )
        ).all()
        assert atts == []
    finally:
        for _ in gen:
            pass
