"""Phase 1e: contact_book + medications + polymorphic ELSIF extension.

Validates parlsr006 deliverables:
- 5 new Class A tables (entries / acks / replies / orders / logs)
- 1 new Class A SELECT-only table (student_allergies)
- parent_owns_attachment gains 2 new owner_types: 'contact_book_entry' /
  'medication_order'
- Subresources (acks / replies / logs) JOIN through their parent entry/order
- Unknown owner_type still fail-closed

Skipped if parlsr006 not applied or ivy_parent_login unreachable.
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

_USER_A = 99401
_USER_B = 99402
_USER_C = 99403
_STUDENT_A = 99401
_STUDENT_B = 99402


def _phase1e_ready() -> bool:
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE tablename IN ('student_contact_book_entries',"
                    "'student_contact_book_acks','student_contact_book_replies',"
                    "'student_medication_orders','student_medication_logs',"
                    "'student_allergies')"
                )
            ).scalar()
        admin_eng.dispose()
        if count < 6:
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
    not _phase1e_ready(),
    reason="parlsr006 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1eContext:
    entry_a_id: int
    entry_b_id: int
    order_a_id: int
    order_b_id: int
    ack_a_id: int
    log_a_id: int


@pytest.fixture(scope="module")
def phase1e_seed() -> Generator[Phase1eContext, None, None]:
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
                    "u": f"phase1e_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1e {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1E-A", "Phase1e A"),
            (_STUDENT_B, "TEST-PHASE1E-B", "Phase1e B"),
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

        # Contact book entries (admin writes; parent reads). NOT NULL:
        # student_id, classroom_id, log_date, version.
        entry_ids = {}
        for sid, key in ((_STUDENT_A, "A"), (_STUDENT_B, "B")):
            res = conn.execute(
                text("""
                INSERT INTO student_contact_book_entries
                  (student_id, classroom_id, log_date, version, published_at, teacher_note)
                VALUES
                  (:s, :c, :d, 1, now(), 'phase1e-seed')
                RETURNING id
                """),
                {"s": sid, "c": classroom_id, "d": date(2026, 5, 1)},
            )
            entry_ids[key] = res.scalar()

        # Ack for entry A (parent A acks their own)
        ack_res = conn.execute(
            text("""
            INSERT INTO student_contact_book_acks
              (entry_id, guardian_user_id, read_at)
            VALUES (:e, :u, :ts)
            RETURNING id
            """),
            {"e": entry_ids["A"], "u": _USER_A, "ts": datetime(2026, 5, 1, 9)},
        )
        ack_a_id = ack_res.scalar()

        # Medication orders. NOT NULL: student_id, order_date, medication_name,
        # dose, time_slots, source.
        order_ids = {}
        for sid, key in ((_STUDENT_A, "A"), (_STUDENT_B, "B")):
            res = conn.execute(
                text("""
                INSERT INTO student_medication_orders
                  (student_id, order_date, medication_name, dose, time_slots, source)
                VALUES
                  (:s, :d, 'phase1e-seed-med', '5ml', '["08:00","20:00"]'::jsonb, 'parent')
                RETURNING id
                """),
                {"s": sid, "d": date(2026, 5, 1)},
            )
            order_ids[key] = res.scalar()

        # Medication log for order A
        log_res = conn.execute(
            text("""
            INSERT INTO student_medication_logs
              (order_id, scheduled_time, skipped)
            VALUES (:o, :t, false)
            RETURNING id
            """),
            {"o": order_ids["A"], "t": "08:00"},
        )
        log_a_id = log_res.scalar()

        # Allergy for student A (admin writes; parent only reads)
        conn.execute(
            text("""
            INSERT INTO student_allergies
              (student_id, allergen, severity, active)
            VALUES (:s, 'phase1e-seed-peanut', 'high', true)
            """),
            {"s": _STUDENT_A},
        )

        # An attachment for entry A (admin-written; parent should see via
        # parent_owns_attachment ELSIF 'contact_book_entry')
        conn.execute(
            text("""
            INSERT INTO attachments
              (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
            VALUES
              ('contact_book_entry', :oid, 'phase1e-cb.jpg', 'phase1e-seed-cb.jpg',
               'image/jpeg', 1024)
            """),
            {"oid": entry_ids["A"]},
        )
        # Same for medication order A
        conn.execute(
            text("""
            INSERT INTO attachments
              (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
            VALUES
              ('medication_order', :oid, 'phase1e-med.jpg', 'phase1e-seed-med.jpg',
               'image/jpeg', 1024)
            """),
            {"oid": order_ids["A"]},
        )

    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    from config import reset_for_tests

    reset_for_tests()
    parent_db.reset_parent_engine_for_tests()

    yield Phase1eContext(
        entry_a_id=entry_ids["A"],
        entry_b_id=entry_ids["B"],
        order_a_id=order_ids["A"],
        order_b_id=order_ids["B"],
        ack_a_id=ack_a_id,
        log_a_id=log_a_id,
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
        text("DELETE FROM attachments WHERE original_filename LIKE 'phase1e-seed%'")
    )
    conn.execute(
        text(
            "DELETE FROM student_medication_logs "
            "WHERE order_id IN (SELECT id FROM student_medication_orders "
            "WHERE medication_name='phase1e-seed-med')"
        )
    )
    conn.execute(
        text(
            "DELETE FROM student_medication_orders WHERE medication_name='phase1e-seed-med'"
        )
    )
    conn.execute(
        text("DELETE FROM student_allergies WHERE allergen='phase1e-seed-peanut'")
    )
    conn.execute(
        text(
            "DELETE FROM student_contact_book_acks "
            "WHERE entry_id IN (SELECT id FROM student_contact_book_entries "
            "WHERE teacher_note='phase1e-seed')"
        )
    )
    conn.execute(
        text(
            "DELETE FROM student_contact_book_replies "
            "WHERE entry_id IN (SELECT id FROM student_contact_book_entries "
            "WHERE teacher_note='phase1e-seed')"
        )
    )
    conn.execute(
        text(
            "DELETE FROM student_contact_book_entries WHERE teacher_note='phase1e-seed'"
        )
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_contact_book_entry_isolated(phase1e_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id FROM student_contact_book_entries WHERE teacher_note='phase1e-seed'"
            )
        ).all()
        assert {r.id for r in rows} == {phase1e_seed.entry_a_id}
    finally:
        for _ in gen:
            pass


def test_contact_book_ack_via_entry_id_join(phase1e_seed):
    """Parent B should NOT see ack tied to parent A's entry."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        rows = session.execute(
            text("SELECT id FROM student_contact_book_acks WHERE entry_id = :e"),
            {"e": phase1e_seed.entry_a_id},
        ).all()
        assert rows == []
    finally:
        for _ in gen:
            pass


def test_contact_book_reply_insert_with_check_enforces_ownership(phase1e_seed):
    """WITH CHECK rejects INSERT pointing at another parent's entry_id."""
    from sqlalchemy.exc import ProgrammingError

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(
                text("""
                INSERT INTO student_contact_book_replies
                  (entry_id, guardian_user_id, body)
                VALUES (:e, :u, 'forge-attempt')
                """),
                {"e": phase1e_seed.entry_b_id, "u": _USER_A},
            )
        assert "row-level security policy" in str(exc.value).lower(), exc.value
    finally:
        try:
            session.rollback()
        except Exception:
            pass
        for _ in gen:
            pass


def test_medication_order_and_log_isolated(phase1e_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        orders = session.execute(
            text(
                "SELECT id FROM student_medication_orders "
                "WHERE medication_name='phase1e-seed-med'"
            )
        ).all()
        assert {o.id for o in orders} == {phase1e_seed.order_a_id}

        # Log JOIN through order_id
        logs = session.execute(
            text("SELECT id FROM student_medication_logs WHERE order_id = :o"),
            {"o": phase1e_seed.order_b_id},
        ).all()
        assert logs == [], "parent A must not see parent B's logs"
    finally:
        for _ in gen:
            pass


def test_allergies_select_only_isolated(phase1e_seed):
    """Parent reads own kids' allergies (for find_allergy_conflicts service)."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT student_id FROM student_allergies "
                "WHERE allergen='phase1e-seed-peanut'"
            )
        ).all()
        assert {r.student_id for r in rows} == {_STUDENT_A}
    finally:
        for _ in gen:
            pass


def test_attachment_polymorphic_elsif_contact_book(phase1e_seed):
    """parent_owns_attachment recognises owner_type='contact_book_entry'."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, owner_type FROM attachments "
                "WHERE original_filename='phase1e-seed-cb.jpg'"
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].owner_type == "contact_book_entry"
    finally:
        for _ in gen:
            pass


def test_attachment_polymorphic_elsif_medication_order(phase1e_seed):
    """parent_owns_attachment recognises owner_type='medication_order'."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id, owner_type FROM attachments "
                "WHERE original_filename='phase1e-seed-med.jpg'"
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].owner_type == "medication_order"
    finally:
        for _ in gen:
            pass


def test_attachment_polymorphic_unknown_owner_type_still_fails_closed(phase1e_seed):
    """Adding 2 ELSIF didn't accidentally open up other owner_types."""
    admin = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text("""
            INSERT INTO attachments
              (owner_type, owner_id, storage_key, original_filename, mime_type, size_bytes)
            VALUES
              ('something_new', :oid, 'phase1e-unk.bin', 'phase1e-seed-unk.bin',
               'application/octet-stream', 1)
            """),
            {"oid": phase1e_seed.entry_a_id},
        )
    admin.dispose()

    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT id FROM attachments WHERE original_filename='phase1e-seed-unk.bin'"
            )
        ).all()
        assert rows == [], f"unknown owner_type leaked: {rows}"
    finally:
        for _ in gen:
            pass


def test_no_guardian_user_sees_nothing(phase1e_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        for sql in (
            "SELECT id FROM student_contact_book_entries WHERE teacher_note='phase1e-seed'",
            "SELECT id FROM student_medication_orders WHERE medication_name='phase1e-seed-med'",
            "SELECT id FROM student_allergies WHERE allergen='phase1e-seed-peanut'",
            "SELECT id FROM attachments WHERE original_filename LIKE 'phase1e-seed%'",
        ):
            rows = session.execute(text(sql)).all()
            assert rows == [], f"leaked rows for: {sql}\n{rows}"
    finally:
        for _ in gen:
            pass
