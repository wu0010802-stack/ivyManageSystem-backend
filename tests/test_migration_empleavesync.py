"""M-1~M-5: empleavesync（employee_leave_attendance_sync）migration 真 PG 行為驗證。

此 migration 是 **PG-only**（``CREATE UNIQUE INDEX CONCURRENTLY`` + ``autocommit_block`` +
``ADD CONSTRAINT USING INDEX``），SQLite 跑不了，故原本 5 個 case 全 ``@skip`` 推給 staging
人工驗。改用 ``tests/_migration_engine``（DATABASE_URL 指 PG 時於隔離 schema 跑真 PG）+ 真
alembic ``Operations``/``MigrationContext``（支援 ``autocommit_block``），讓 CI 的 ``test`` /
``alembic-roundtrip`` job（皆設 DATABASE_URL=PG）自動驗；本地無 PG 時整檔 skip。

最小 pre-migration schema（**不** ``Base.metadata.create_all``——那會給含 ``leave_record_id``
的最新 schema 致 DuplicateColumn）：``attendances(id, employee_id, attendance_date)`` +
``leave_records(...)``。
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Integer,
    MetaData,
    Numeric,
    Table,
    Time,
    inspect,
    text,
)

from tests._migration_engine import is_postgres, make_migration_engine

pytestmark = pytest.mark.skipif(
    not is_postgres(),
    reason="empleavesync 是 PG-only migration（CONCURRENTLY/autocommit_block），需 DATABASE_URL 指向 Postgres",
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "empleavesync_employee_leave_attendance_sync.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("empleavesync_mig", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_pre_migration_schema(engine) -> None:
    """建 migration 前的最小 schema（attendances 尚無 leave_record_id / 唯一約束）。"""
    md = MetaData()
    Table(
        "attendances",
        md,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer, nullable=False),
        Column("attendance_date", Date, nullable=False),
    )
    Table(
        "leave_records",
        md,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer, nullable=True),
        Column("start_date", Date, nullable=True),
        Column("end_date", Date, nullable=True),
        Column("start_time", Time, nullable=True),
        Column("end_time", Time, nullable=True),
        Column("leave_hours", Numeric(4, 2), nullable=True),
        Column("is_approved", Boolean, nullable=True),
    )
    md.create_all(engine)


def _drive(engine, migration, direction: str) -> None:
    """以真 alembic Operations/MigrationContext 跑 migration（支援 autocommit_block +
    CREATE INDEX CONCURRENTLY），近似 alembic 實際 runner。"""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    conn = engine.connect()
    try:
        ctx = MigrationContext.configure(conn)
        op_obj = Operations(ctx)
        old_op = getattr(migration, "op", None)
        migration.op = op_obj
        try:
            with ctx.begin_transaction():
                getattr(migration, direction)()
        finally:
            migration.op = old_op
    finally:
        conn.close()


def _attendance_state(engine) -> tuple[set, set, set, set]:
    with engine.connect() as conn:
        insp = inspect(conn)
        cols = {c["name"] for c in insp.get_columns("attendances")}
        uniques = {u["name"] for u in insp.get_unique_constraints("attendances")}
        indexes = {i["name"] for i in insp.get_indexes("attendances")}
        fks = {fk["name"] for fk in insp.get_foreign_keys("attendances")}
    return cols, uniques, indexes, fks


def test_m1_upgrade_clean_db(tmp_path, monkeypatch):
    """M-1：clean DB upgrade → 加欄 + FK + 唯一約束 + 索引，無報錯。"""
    monkeypatch.setenv("IVY_SKIP_BACKFILL", "1")
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_empleavesync_m1")
    try:
        _create_pre_migration_schema(engine)
        _drive(engine, _load_migration(), "upgrade")

        cols, uniques, indexes, fks = _attendance_state(engine)
        assert "leave_record_id" in cols
        assert "partial_leave_hours" in cols
        assert "uq_attendance_employee_date" in uniques
        assert "ix_attendance_leave_record_id" in indexes
        assert "fk_attendance_leave" in fks
    finally:
        cleanup()


def test_m2_upgrade_with_dups_fails_loud(tmp_path, monkeypatch):
    """M-2：(employee_id, attendance_date) 有重複 → fail-loud RuntimeError。"""
    monkeypatch.setenv("IVY_SKIP_BACKFILL", "1")
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_empleavesync_m2")
    try:
        _create_pre_migration_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO attendances (id, employee_id, attendance_date) "
                    "VALUES (1, 100, '2026-05-01'), (2, 100, '2026-05-01')"
                )
            )
        with pytest.raises(RuntimeError, match="重複"):
            _drive(engine, _load_migration(), "upgrade")
    finally:
        cleanup()


def test_m3_backfill_runs_on_pg_with_no_approved_leaves(tmp_path, monkeypatch):
    """M-3：不設 IVY_SKIP_BACKFILL，backfill 程式路徑在真 PG 跑（0 筆 approved leave）→
    SELECT / 迴圈 / imports 皆通過、upgrade 完成。完整 backfill（含 apply()）需 sync-service
    fixture，超出本檔範圍，故以「空 approved 集」驗 backfill SQL 與 imports 在 PG 可執行。"""
    monkeypatch.delenv("IVY_SKIP_BACKFILL", raising=False)
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_empleavesync_m3")
    try:
        _create_pre_migration_schema(engine)
        _drive(engine, _load_migration(), "upgrade")
        cols, _, _, _ = _attendance_state(engine)
        assert "leave_record_id" in cols
    finally:
        cleanup()


@pytest.mark.skip(
    reason="此 migration 對『完整 upgrade 重跑』非冪等（op.add_column 在欄已存在時 "
    "DuplicateColumn）；僅 CONCURRENTLY 索引段為 IF NOT EXISTS。alembic 以 version "
    "stamp 防重跑，非靠 migration 自身冪等，故 M-4 全 re-run 假設不成立。"
)
def test_m4_upgrade_idempotent():
    pass


def test_m5_downgrade_restores_schema(tmp_path, monkeypatch):
    """M-5：upgrade 後 downgrade → 加的欄 / FK / 唯一約束移除（schema 還原）。"""
    monkeypatch.setenv("IVY_SKIP_BACKFILL", "1")
    engine, cleanup = make_migration_engine(tmp_path, schema="mig_empleavesync_m5")
    try:
        _create_pre_migration_schema(engine)
        migration = _load_migration()
        _drive(engine, migration, "upgrade")
        _drive(engine, migration, "downgrade")

        cols, uniques, _, fks = _attendance_state(engine)
        assert "leave_record_id" not in cols
        assert "partial_leave_hours" not in cols
        assert "uq_attendance_employee_date" not in uniques
        assert "fk_attendance_leave" not in fks
    finally:
        cleanup()
