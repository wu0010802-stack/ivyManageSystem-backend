"""驗證 offb0001 migration 建立表結構與 SalaryRecord.unused_leave_payout 欄。

採用 _AlembicOpStub 直接對 SQLite 執行 upgrade()/downgrade()，
與既有 test_recruitment_ivykids_migration.py 同一模式。
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "offb0001_employee_offboarding_records.py"
)


class _AlembicOpStub:
    """仿 alembic.op 介面，實際操作 SQLite connection。"""

    def __init__(self, bind):
        self.bind = bind
        self._metadata = MetaData()

    def get_context(self):
        class _Ctx:
            class dialect:
                name = "sqlite"

        return _Ctx()

    def create_table(self, table_name, *columns, **kwargs):
        meta = MetaData()
        # SQLite 不強制 FK，略過 ForeignKeyConstraint / PrimaryKeyConstraint
        # 只保留 Column（PK 欄位本身已帶 primary_key=True）
        cols = [c for c in columns if isinstance(c, sa.Column)]
        # 移除 Column 上的 ForeignKey 避免 SA 嘗試解析外表
        clean_cols = []
        for col in cols:
            new_col = col.copy()
            new_col.foreign_keys = set()
            clean_cols.append(new_col)
        Table(table_name, meta, *clean_cols)
        meta.create_all(self.bind)

    def create_index(
        self,
        index_name,
        table_name,
        columns,
        unique=False,
        **kwargs,
    ):
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=self.bind)
        idx_cols = [table.c[c] for c in columns]
        # partial index (sqlite_where / postgresql_where) — SQLite 支援 partial
        sqlite_where = kwargs.get("sqlite_where")
        postgresql_where = kwargs.get("postgresql_where")
        where_clause = sqlite_where if sqlite_where is not None else postgresql_where
        if where_clause is not None:
            idx = sa.Index(
                index_name, *idx_cols, unique=unique, sqlite_where=where_clause
            )
        else:
            idx = sa.Index(index_name, *idx_cols, unique=unique)
        idx.create(self.bind)

    def add_column(self, table_name, column):
        col_type = column.type.compile(dialect=self.bind.dialect)
        server_default = ""
        if column.server_default is not None:
            sd = column.server_default
            val = sd.arg if hasattr(sd, "arg") else sd
            server_default = f" DEFAULT {val}"
        nullable = "" if column.nullable else " NOT NULL"
        comment = ""
        self.bind.execute(
            text(
                f"ALTER TABLE {table_name} ADD COLUMN "
                f"{column.name} {col_type}{server_default}{nullable}{comment}"
            )
        )

    def drop_column(self, table_name, column_name):
        self.bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    def drop_index(self, index_name, table_name=None, **kwargs):
        self.bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))

    def drop_table(self, table_name):
        self.bind.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("offb0001", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prerequisite_tables(bind):
    """建立 migration 需要的前置表（employees, users, salary_records）。"""
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
        Column("base_salary", Numeric(12, 2), server_default="0"),
    )
    meta.create_all(bind)


def test_offb0001_creates_table_and_indexes(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        insp = inspect(conn)
        assert "employee_offboarding_records" in insp.get_table_names()

        cols = {c["name"] for c in insp.get_columns("employee_offboarding_records")}
        expected_cols = {
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
        assert expected_cols.issubset(cols), f"missing: {expected_cols - cols}"

        indexes = {i["name"] for i in insp.get_indexes("employee_offboarding_records")}
        assert "ix_offboarding_resign_date" in indexes
        assert "ix_offboarding_open_status" in indexes

    engine.dispose()


def test_offb0001_adds_unused_leave_payout_column(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        insp = inspect(conn)
        cols = {c["name"] for c in insp.get_columns("salary_records")}
        assert "unused_leave_payout" in cols

    engine.dispose()


def test_offb0001_downgrade_drops_table_and_column(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)
        stub = _AlembicOpStub(conn)
        module.op = stub
        module.upgrade()

        # verify upgraded state
        insp = inspect(conn)
        assert "employee_offboarding_records" in insp.get_table_names()

    # downgrade in new transaction (SQLite DDL not transactional in same conn)
    engine2 = create_engine(db_url)
    with engine2.begin() as conn2:
        module2 = _load_migration_module()
        module2.op = _AlembicOpStub(conn2)
        module2.downgrade()

        insp2 = inspect(conn2)
        assert "employee_offboarding_records" not in insp2.get_table_names()
        cols = {c["name"] for c in insp2.get_columns("salary_records")}
        assert "unused_leave_payout" not in cols

    engine.dispose()
    engine2.dispose()
