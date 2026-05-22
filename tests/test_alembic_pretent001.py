"""pretent001 migration 回歸測試。

測試覆蓋：
1. upgrade 新增 terminal_entered_at + pii_redacted_at 欄位
2. downgrade 移除該兩欄位
3. backfill：已終態的學生 terminal_entered_at = updated_at（SQLite path）

Reference: tests/test_recruitment_ivykids_migration.py
"""

import importlib.util
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
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
    / "20260522_pretent001_pii_retention_columns.py"
)


class _AlembicOpStub:
    """讓 migration 內 op.* 在測試環境下操作 SQLite test connection。"""

    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind

    def add_column(self, table_name, column):
        """ALTER TABLE ... ADD COLUMN（SQLite 3.35+ 支援）。"""
        col_type = column.type.compile(dialect=self.bind.dialect)
        nullable = "" if column.nullable else " NOT NULL"
        self.bind.execute(
            text(
                f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}{nullable}"
            )
        )

    def drop_column(self, table_name, column_name):
        """ALTER TABLE ... DROP COLUMN（SQLite 3.35+）。"""
        self.bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    def create_index(self, index_name, table_name, columns, unique=False, **kwargs):
        """CREATE INDEX（SQLite 不支援 partial index，忽略 postgresql_where 等 kwargs）。"""
        cols = ", ".join(columns)
        unique_kw = "UNIQUE " if unique else ""
        self.bind.execute(
            text(
                f"CREATE {unique_kw}INDEX IF NOT EXISTS {index_name} ON {table_name} ({cols})"
            )
        )

    def drop_index(self, index_name, table_name=None):
        self.bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))

    def execute(self, sql):
        """執行 SQL 字串或 TextClause。"""
        if isinstance(sql, str):
            sql = text(sql)
        self.bind.execute(sql)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "pretent001_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_pretent001_prereq_tables(bind):
    """建 migration 會碰到的最小 students / guardians / audit_logs 表並插入測試資料。"""
    metadata = MetaData()

    students = Table(
        "students",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
        Column("lifecycle_status", String(20), nullable=False, server_default="active"),
        Column("updated_at", DateTime, nullable=True),
        Column("created_at", DateTime, nullable=True),
        Column("birth_date", DateTime, nullable=True),
    )
    Table(
        "guardians",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("student_id", Integer, nullable=False),
        Column("name", String(50), nullable=False),
        Column("deleted_at", DateTime, nullable=True),
    )
    Table(
        "audit_logs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("entity_type", String(50), nullable=True),
        Column("entity_id", String(50), nullable=True),
        Column("action", String(20), nullable=True),
        Column("changes", Text, nullable=True),
        Column("summary", Text, nullable=True),
        Column("created_at", DateTime, nullable=True),
    )
    metadata.create_all(bind)

    # 插入測試資料：3 個學生
    bind.execute(
        students.insert(),
        [
            # 已畢業，updated_at 設為特定時間，用來驗 backfill
            {
                "id": 1,
                "name": "畢業生甲",
                "lifecycle_status": "graduated",
                "updated_at": datetime(2025, 6, 30, 12, 0, 0),
                "created_at": datetime(2024, 9, 1, 8, 0, 0),
            },
            # 已轉出
            {
                "id": 2,
                "name": "轉出生乙",
                "lifecycle_status": "transferred",
                "updated_at": datetime(2025, 3, 15, 9, 0, 0),
                "created_at": datetime(2024, 9, 1, 8, 0, 0),
            },
            # 在學中（active），不應被 backfill
            {
                "id": 3,
                "name": "在學生丙",
                "lifecycle_status": "active",
                "updated_at": datetime(2026, 1, 10, 8, 0, 0),
                "created_at": datetime(2024, 9, 1, 8, 0, 0),
            },
        ],
    )


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_upgrade_adds_columns(tmp_path):
    """upgrade 後 students 有 terminal_entered_at、guardians 有 pii_redacted_at。"""
    db_path = tmp_path / "pretent001_upgrade.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_pretent001_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        inspector = inspect(conn)
        student_cols = {c["name"] for c in inspector.get_columns("students")}
        guardian_cols = {c["name"] for c in inspector.get_columns("guardians")}

        assert (
            "terminal_entered_at" in student_cols
        ), "students 應有 terminal_entered_at 欄位"
        assert "pii_redacted_at" in guardian_cols, "guardians 應有 pii_redacted_at 欄位"

    engine.dispose()


def test_downgrade_drops_columns(tmp_path):
    """upgrade → downgrade 後兩欄位消失。"""
    db_path = tmp_path / "pretent001_downgrade.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_pretent001_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)

        module.upgrade()
        module.downgrade()

        inspector = inspect(conn)
        student_cols = {c["name"] for c in inspector.get_columns("students")}
        guardian_cols = {c["name"] for c in inspector.get_columns("guardians")}

        assert (
            "terminal_entered_at" not in student_cols
        ), "downgrade 後 students 不應有 terminal_entered_at"
        assert (
            "pii_redacted_at" not in guardian_cols
        ), "downgrade 後 guardians 不應有 pii_redacted_at"

    engine.dispose()


def test_backfill_uses_updated_at(tmp_path):
    """已終態的學生 terminal_entered_at 被 backfill 為其 updated_at。在學生不受影響。"""
    db_path = tmp_path / "pretent001_backfill.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_pretent001_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        rows = (
            conn.execute(
                text(
                    "SELECT id, lifecycle_status, terminal_entered_at, updated_at "
                    "FROM students ORDER BY id"
                )
            )
            .mappings()
            .all()
        )

        # 畢業生（id=1）：terminal_entered_at 應 = updated_at
        graduated = next(r for r in rows if r["id"] == 1)
        assert (
            graduated["terminal_entered_at"] is not None
        ), "已畢業學生應被 backfill terminal_entered_at"
        assert (
            graduated["terminal_entered_at"] == graduated["updated_at"]
        ), "SQLite path 應以 updated_at 為 backfill 值"

        # 轉出生（id=2）
        transferred = next(r for r in rows if r["id"] == 2)
        assert (
            transferred["terminal_entered_at"] is not None
        ), "已轉出學生應被 backfill terminal_entered_at"
        assert transferred["terminal_entered_at"] == transferred["updated_at"]

        # 在學生（id=3）：不應被 backfill
        active = next(r for r in rows if r["id"] == 3)
        assert (
            active["terminal_entered_at"] is None
        ), "在學學生的 terminal_entered_at 應保持 NULL"

    engine.dispose()
