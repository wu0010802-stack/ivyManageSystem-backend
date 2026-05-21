"""Phase 1c: fees + measurements + growth_reports isolation against real PG.

Validates parlsr004 deliverables on 6 RLS-enabled tables:
- student_fee_records (Class A direct)
- student_fee_payments (Class A via record_id JOIN)
- student_fee_adjustments (Class A direct)
- student_fee_refunds (Class A via record_id JOIN)
- student_measurements (Class A direct)
- student_growth_reports (Class A direct, FOR ALL — supports UPDATE for view_count)

Skipped if parlsr004 not applied or ivy_parent_login unreachable.
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

# Disjoint from prior phase seed IDs (99001 + 99101 used by 1 / 1b).
_USER_A = 99201
_USER_B = 99202
_USER_C = 99203  # no children
_STUDENT_A = 99201
_STUDENT_B = 99202


def _phase1c_ready() -> bool:
    try:
        admin_eng = create_engine(_ADMIN_URL)
        with admin_eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE tablename IN ('student_fee_records','student_fee_payments',"
                    "'student_fee_adjustments','student_fee_refunds',"
                    "'student_measurements','student_growth_reports')"
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
    not _phase1c_ready(),
    reason="parlsr004 not applied OR ivy_parent_login password mismatch",
)


@dataclass
class Phase1cContext:
    record_a_id: int
    record_b_id: int
    growth_a_id: int


@pytest.fixture(scope="module")
def phase1c_seed() -> Generator[Phase1cContext, None, None]:
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
                    "u": f"phase1c_test_user_{uid}",
                    "pw": "x" * 60,
                    "name": f"Phase1c Test {uid}",
                },
            )

        classroom_id = conn.execute(
            text("SELECT id FROM classrooms ORDER BY id LIMIT 1")
        ).scalar()
        for sid, code, name in (
            (_STUDENT_A, "TEST-PHASE1C-A", "Phase1c A"),
            (_STUDENT_B, "TEST-PHASE1C-B", "Phase1c B"),
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

        # Fee records — direct student_id; NOT NULL: student_name, period
        record_ids = {}
        for sid, sname, key in (
            (_STUDENT_A, "Phase1c A", "A"),
            (_STUDENT_B, "Phase1c B", "B"),
        ):
            res = conn.execute(
                text("""
                INSERT INTO student_fee_records
                  (student_id, student_name, fee_item_name, period, amount_due, amount_paid, status)
                VALUES
                  (:s, :sn, 'phase1c-seed', '2026-05', 1000, 0, 'unpaid')
                RETURNING id
                """),
                {"s": sid, "sn": sname},
            )
            record_ids[key] = res.scalar()

        # Fee payment + refund + adjustment for record A.
        # refunded_by NOT NULL — use one of our test users.
        # student_fee_adjustments: period + adjustment_type NOT NULL.
        conn.execute(
            text("""
            INSERT INTO student_fee_payments
              (record_id, amount, payment_date, payment_method, idempotency_key)
            VALUES (:r, 500, '2026-05-10', 'cash', 'phase1c-seed-receipt-A')
            """),
            {"r": record_ids["A"]},
        )
        conn.execute(
            text("""
            INSERT INTO student_fee_refunds
              (record_id, amount, reason, refunded_by, refunded_at)
            VALUES (:r, 100, 'phase1c-seed-refund', :ub, :ts)
            """),
            {"r": record_ids["A"], "ub": _USER_A, "ts": datetime(2026, 5, 11)},
        )
        conn.execute(
            text("""
            INSERT INTO student_fee_adjustments
              (student_id, amount, reason, period, adjustment_type)
            VALUES (:s, 50, 'phase1c-seed-adjustment', '2026-05', 'discount')
            """),
            {"s": _STUDENT_A},
        )

        # Measurement for student A
        conn.execute(
            text("""
            INSERT INTO student_measurements
              (student_id, measured_on, height_cm, weight_kg, note)
            VALUES (:s, '2026-05-15', 110.5, 18.2, 'phase1c-seed')
            """),
            {"s": _STUDENT_A},
        )

        # Growth report for student A — status='ready' so parent can see it
        res = conn.execute(
            text("""
            INSERT INTO student_growth_reports
              (student_id, period_label, period_start, period_end, status,
               file_path, file_size, generated_at, parent_view_count)
            VALUES
              (:s, 'phase1c-seed', '2026-01-01', '2026-05-31', 'ready',
               '/tmp/phase1c.pdf', 1024, :ts, 0)
            RETURNING id
            """),
            {"s": _STUDENT_A, "ts": datetime(2026, 5, 18)},
        )
        growth_a_id = res.scalar()

    os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
    os.environ["PARENT_DB_USER"] = "ivy_parent_login"
    os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
    from config import reset_for_tests

    reset_for_tests()
    parent_db.reset_parent_engine_for_tests()

    yield Phase1cContext(
        record_a_id=record_ids["A"],
        record_b_id=record_ids["B"],
        growth_a_id=growth_a_id,
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
    # Order matters: children before parents
    conn.execute(
        text(
            "DELETE FROM student_fee_payments WHERE idempotency_key LIKE 'phase1c-seed%'"
        )
    )
    conn.execute(
        text("DELETE FROM student_fee_refunds WHERE reason LIKE 'phase1c-seed%'")
    )
    conn.execute(
        text("DELETE FROM student_fee_adjustments WHERE reason LIKE 'phase1c-seed%'")
    )
    conn.execute(
        text("DELETE FROM student_fee_records WHERE fee_item_name='phase1c-seed'")
    )
    conn.execute(text("DELETE FROM student_measurements WHERE note='phase1c-seed'"))
    conn.execute(
        text("DELETE FROM student_growth_reports WHERE period_label='phase1c-seed'")
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


def test_fee_records_isolated(phase1c_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        ids = {
            row.id
            for row in session.execute(
                text("SELECT id FROM student_fee_records WHERE id IN (:a, :b)"),
                {"a": phase1c_seed.record_a_id, "b": phase1c_seed.record_b_id},
            ).all()
        }
        assert ids == {phase1c_seed.record_a_id}
    finally:
        for _ in gen:
            pass


def test_fee_payments_and_refunds_isolated_via_join(phase1c_seed):
    """payments + refunds have no direct student_id; JOIN through record_id
    must respect parent ownership."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        # B's record has no seeded payment/refund (those belong to A's record)
        payments = session.execute(
            text("SELECT id FROM student_fee_payments " "WHERE record_id = :r"),
            {"r": phase1c_seed.record_a_id},
        ).all()
        assert (
            payments == []
        ), f"parent B must NOT see payments tied to parent A's record; saw {payments}"

        refunds = session.execute(
            text("SELECT id FROM student_fee_refunds WHERE record_id = :r"),
            {"r": phase1c_seed.record_a_id},
        ).all()
        assert refunds == [], refunds
    finally:
        for _ in gen:
            pass


def test_fee_adjustments_direct_student_id(phase1c_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        rows = session.execute(
            text(
                "SELECT student_id FROM student_fee_adjustments "
                "WHERE reason = 'phase1c-seed-adjustment'"
            )
        ).all()
        assert {r.student_id for r in rows} == {_STUDENT_A}
    finally:
        for _ in gen:
            pass


def test_measurements_isolated(phase1c_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_B)
    session = next(gen)
    try:
        # B's child has no seeded measurement
        rows = session.execute(
            text("SELECT id FROM student_measurements WHERE note='phase1c-seed'")
        ).all()
        assert rows == [], rows
    finally:
        for _ in gen:
            pass


def test_growth_reports_isolated_and_updatable_by_owner(phase1c_seed):
    """Parent A can SELECT own report AND UPDATE view_count (FOR ALL policy)."""
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_A)
    session = next(gen)
    try:
        # Read
        before = session.execute(
            text("SELECT parent_view_count FROM student_growth_reports WHERE id = :i"),
            {"i": phase1c_seed.growth_a_id},
        ).scalar()
        assert before == 0

        # Atomic INCR — same idiom as parent_download_report uses
        session.execute(
            text("""
            UPDATE student_growth_reports
            SET parent_view_count = COALESCE(parent_view_count, 0) + 1
            WHERE id = :i
            """),
            {"i": phase1c_seed.growth_a_id},
        )
        session.flush()

        after = session.execute(
            text("SELECT parent_view_count FROM student_growth_reports WHERE id = :i"),
            {"i": phase1c_seed.growth_a_id},
        ).scalar()
        assert after == 1
    finally:
        for _ in gen:
            pass


def test_no_guardian_user_sees_nothing(phase1c_seed):
    engine = parent_db.get_parent_engine()
    gen = parent_db.build_parent_session_for_user(engine, _USER_C)
    session = next(gen)
    try:
        for table, where in (
            ("student_fee_records", "fee_item_name='phase1c-seed'"),
            ("student_fee_adjustments", "reason='phase1c-seed-adjustment'"),
            ("student_measurements", "note='phase1c-seed'"),
            ("student_growth_reports", "period_label='phase1c-seed'"),
        ):
            rows = session.execute(text(f"SELECT id FROM {table} WHERE {where}")).all()
            assert rows == [], f"{table} leaked: {rows}"
    finally:
        for _ in gen:
            pass
