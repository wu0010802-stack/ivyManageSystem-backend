"""驗證 offb0001 migration（employee_offboarding_records + SalaryRecord.unused_leave_payout）。

改用 ``tests/_migration_engine``：``run_migration`` 以**真 alembic Operations/MigrationContext**
跑 upgrade/downgrade（``op.get_context().dialect.name`` 回報真實 dialect → migration 的
``postgresql_where`` / ``sqlite_where`` 分支走對），engine 由 ``make_migration_engine`` 提供：
``DATABASE_URL`` 指 PG（CI ``test`` / ``alembic-roundtrip`` job）時於隔離 schema 跑真 PG，
否則 tmp sqlite。故同一份測試在兩個 dialect 都驗（取代原硬編 ``dialect.name="sqlite"`` 的
``_AlembicOpStub``，那在 PG 上會誤走 sqlite 分支）。
"""

import importlib.util
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    inspect,
    text,
)

from tests._migration_engine import make_migration_engine, run_migration

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "offb0001_employee_offboarding_records.py"
)

_EXPECTED_OFFBOARDING_COLS = {
    "employee_id",
    "resign_date",
    "resign_reason",
    "opened_at",
    "opened_by_user_id",
    "user_revoked_at",
    "appraisal_marked_at",
    "leave_snapshot_at",
    "certificate_generated_at",
    "leave_balance_snapshot",
    "certificate_pdf_path",
    "nhi_unenroll_submitted_at",
    "magic_link_token_hash",
    "magic_link_expires_at",
    "magic_link_revoked_at",
    "magic_link_download_count",
    "magic_link_last_used_at",
    "closed_at",
    "closed_by_user_id",
}


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("offb0001", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prerequisite_tables(engine) -> None:
    """建 migration 的前置表（employees / users / salary_records）。

    employee_offboarding_records 對 employees / users 有 FK；salary_records 被 add_column。
    """
    meta = MetaData()
    Table(
        "employees",
        meta,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
    )
    Table(
        "users",
        meta,
        Column("id", Integer, primary_key=True),
        Column("username", String(50)),
    )
    Table(
        "salary_records",
        meta,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer),
        Column("salary_year", Integer),
        Column("salary_month", Integer),
        Column("base_salary", Numeric(12, 2), server_default=text("0")),
    )
    meta.create_all(engine)


def test_offb0001_creates_table_and_indexes(tmp_path):
    """upgrade → 建 employee_offboarding_records（含全部欄）+ 兩個索引。"""
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_offb_create")
    try:
        _create_prerequisite_tables(engine)
        run_migration(engine, _load_migration_module(), "upgrade")

        with engine.connect() as conn:
            insp = inspect(conn)
            assert "employee_offboarding_records" in insp.get_table_names()
            cols = {c["name"] for c in insp.get_columns("employee_offboarding_records")}
            assert _EXPECTED_OFFBOARDING_COLS.issubset(cols), (
                f"missing: {_EXPECTED_OFFBOARDING_COLS - cols}"
            )
            indexes = {
                i["name"] for i in insp.get_indexes("employee_offboarding_records")
            }
            assert "ix_offboarding_resign_date" in indexes
            assert "ix_offboarding_open_status" in indexes
    finally:
        cleanup()


def test_offb0001_adds_unused_leave_payout_column(tmp_path):
    """upgrade → salary_records 加 unused_leave_payout 欄。"""
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_offb_addcol")
    try:
        _create_prerequisite_tables(engine)
        run_migration(engine, _load_migration_module(), "upgrade")

        with engine.connect() as conn:
            cols = {c["name"] for c in inspect(conn).get_columns("salary_records")}
            assert "unused_leave_payout" in cols
    finally:
        cleanup()


def test_offb0001_downgrade_drops_table_and_column(tmp_path):
    """upgrade 後 downgrade → 表與欄移除（schema 還原）。"""
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_offb_downgrade")
    try:
        _create_prerequisite_tables(engine)
        module = _load_migration_module()
        run_migration(engine, module, "upgrade")
        with engine.connect() as conn:
            assert "employee_offboarding_records" in inspect(conn).get_table_names()

        run_migration(engine, module, "downgrade")
        with engine.connect() as conn:
            insp = inspect(conn)
            assert "employee_offboarding_records" not in insp.get_table_names()
            cols = {c["name"] for c in insp.get_columns("salary_records")}
            assert "unused_leave_payout" not in cols
    finally:
        cleanup()
