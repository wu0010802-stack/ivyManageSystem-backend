"""Spike RLS test fixtures — requires real PostgreSQL (NOT SQLite).

Creates a `rls_spike` schema in the dev database, plus two roles
(`rls_spike_parent_login` / `rls_spike_admin_login`), seeds two parent/child
pairs, applies a Class A RLS policy on `student_attendance`, and yields
connection URLs the tests can use.

Tear down: DROP SCHEMA CASCADE + DROP ROLE.

Skip these tests on SQLite environments (parent conftest patches JSONB/BigInt
for SQLite — those patches are harmless here because spike uses raw SQL).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Generator

import pytest
from sqlalchemy import create_engine, text

# Reuse local dev PG; superuser yilunwu can CREATE ROLE / CREATE SCHEMA.
ADMIN_URL = os.environ.get(
    "RLS_SPIKE_ADMIN_URL",
    "postgresql://yilunwu@localhost:5432/ivymanagement",
)
PARENT_LOGIN_PW = "spike_parent_pw_2026_05_18"
ADMIN_LOGIN_PW = "spike_admin_pw_2026_05_18"


def _build_parent_url() -> str:
    return ADMIN_URL.replace(
        "postgresql://yilunwu@",
        f"postgresql://rls_spike_parent_login:{PARENT_LOGIN_PW}@",
    )


def _build_admin_login_url() -> str:
    return ADMIN_URL.replace(
        "postgresql://yilunwu@",
        f"postgresql://rls_spike_admin_login:{ADMIN_LOGIN_PW}@",
    )


@dataclass
class SpikeContext:
    admin_url: str  # superuser (peer auth)
    parent_url: str  # rls_spike_parent_login (RLS-bound)
    admin_login_url: str  # rls_spike_admin_login (BYPASSRLS)
    parent_a_user_id: int
    parent_b_user_id: int
    student_a_id: int
    student_b_id: int
    no_child_user_id: int
    schema: str = "rls_spike"


@pytest.fixture(scope="session")
def spike_pg() -> Generator[SpikeContext, None, None]:
    """Provision spike schema/roles/data once per pytest session."""
    admin_engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        # Clean any prior failed run
        conn.execute(text("DROP SCHEMA IF EXISTS rls_spike CASCADE"))
        for role in ("rls_spike_parent_login", "rls_spike_admin_login"):
            conn.execute(
                text(
                    f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}')"
                    f" THEN DROP ROLE {role}; END IF; END $$"
                )
            )
        for role in ("rls_spike_parent_role", "rls_spike_admin_role"):
            conn.execute(
                text(
                    f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}')"
                    f" THEN DROP ROLE {role}; END IF; END $$"
                )
            )

        # Create roles. NOTE: BYPASSRLS requires superuser to grant.
        # **BYPASSRLS is NOT inherited** through role membership (PG treats it
        # the same as SUPERUSER/CREATEDB/LOGIN — never inherited). So the
        # login role must carry BYPASSRLS itself, not just its group role.
        conn.execute(text("CREATE ROLE rls_spike_parent_role NOLOGIN"))
        conn.execute(text("CREATE ROLE rls_spike_admin_role NOLOGIN BYPASSRLS"))
        conn.execute(
            text(
                f"CREATE ROLE rls_spike_parent_login WITH LOGIN PASSWORD '{PARENT_LOGIN_PW}' IN ROLE rls_spike_parent_role"
            )
        )
        conn.execute(
            text(
                f"CREATE ROLE rls_spike_admin_login WITH LOGIN PASSWORD '{ADMIN_LOGIN_PW}' BYPASSRLS IN ROLE rls_spike_admin_role"
            )
        )

        # Build spike schema
        conn.execute(text("CREATE SCHEMA rls_spike"))
        conn.execute(text("""
            CREATE TABLE rls_spike.guardian (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                deleted_at TIMESTAMP
            )
            """))
        conn.execute(text("""
            CREATE INDEX ix_rls_spike_guardian_user_active
            ON rls_spike.guardian (user_id, student_id)
            WHERE deleted_at IS NULL
            """))
        conn.execute(text("""
            CREATE TABLE rls_spike.student_attendance (
                id SERIAL PRIMARY KEY,
                student_id INTEGER NOT NULL,
                check_in_at TIMESTAMP NOT NULL DEFAULT now(),
                note TEXT
            )
            """))

        # GRANT (precise — Class A read-only example)
        conn.execute(
            text(
                "GRANT USAGE ON SCHEMA rls_spike TO rls_spike_parent_role, rls_spike_admin_role"
            )
        )
        conn.execute(
            text(
                "GRANT SELECT ON rls_spike.guardian, rls_spike.student_attendance "
                "TO rls_spike_parent_role"
            )
        )
        conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE ON rls_spike.guardian, rls_spike.student_attendance "
                "TO rls_spike_admin_role"
            )
        )
        conn.execute(
            text(
                "GRANT USAGE ON ALL SEQUENCES IN SCHEMA rls_spike "
                "TO rls_spike_parent_role, rls_spike_admin_role"
            )
        )

        # ENABLE + FORCE RLS, plus the Class A policy
        conn.execute(
            text("ALTER TABLE rls_spike.student_attendance ENABLE ROW LEVEL SECURITY")
        )
        conn.execute(
            text("ALTER TABLE rls_spike.student_attendance FORCE ROW LEVEL SECURITY")
        )
        conn.execute(text("""
            CREATE POLICY parent_isolate_attendance ON rls_spike.student_attendance
            FOR ALL TO rls_spike_parent_role
            USING (
                student_id IN (
                    SELECT g.student_id FROM rls_spike.guardian g
                    WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                      AND g.deleted_at IS NULL
                )
            )
            WITH CHECK (
                student_id IN (
                    SELECT g.student_id FROM rls_spike.guardian g
                    WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                      AND g.deleted_at IS NULL
                )
            )
            """))
        # Guardian table also needs RLS so a parent can't enumerate other parents'
        # children rows. Policy mirrors the user_id direct-match pattern.
        conn.execute(text("ALTER TABLE rls_spike.guardian ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE rls_spike.guardian FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            CREATE POLICY parent_isolate_guardian ON rls_spike.guardian
            FOR ALL TO rls_spike_parent_role
            USING (
                user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            )
            """))

        # Seed data: parent A (user_id=101) -> student 201; parent B (102) -> 202;
        # plus parent C (103) with no child.
        conn.execute(text("""
            INSERT INTO rls_spike.guardian (user_id, student_id) VALUES
                (101, 201), (102, 202)
            """))
        conn.execute(text("""
            INSERT INTO rls_spike.student_attendance (student_id, note) VALUES
                (201, 'A-day1'), (201, 'A-day2'), (202, 'B-day1'), (202, 'B-day2')
            """))

    admin_engine.dispose()

    ctx = SpikeContext(
        admin_url=ADMIN_URL,
        parent_url=_build_parent_url(),
        admin_login_url=_build_admin_login_url(),
        parent_a_user_id=101,
        parent_b_user_id=102,
        student_a_id=201,
        student_b_id=202,
        no_child_user_id=103,
    )
    yield ctx

    # Teardown
    cleanup_engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with cleanup_engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS rls_spike CASCADE"))
        for role in (
            "rls_spike_parent_login",
            "rls_spike_admin_login",
            "rls_spike_parent_role",
            "rls_spike_admin_role",
        ):
            conn.execute(
                text(
                    f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='{role}')"
                    f" THEN DROP ROLE {role}; END IF; END $$"
                )
            )
    cleanup_engine.dispose()
