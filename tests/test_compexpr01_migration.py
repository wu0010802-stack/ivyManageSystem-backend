"""驗證 compexpr01 upgrade/downgrade 對稱 + backfill 正確。

採用 _AlembicOpStub 直接對 SQLite 執行 upgrade()/downgrade()，
與既有 test_offboarding_migration.py 同一模式。
"""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
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
    / "20260526_compexpr01_leave_quota_lifecycle.py"
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

    def get_bind(self):
        return self.bind

    @staticmethod
    def _sqlite_server_default(col) -> str | None:
        """SA server_default → SQLite-compatible DEFAULT clause value，或 None 略過。"""
        sd = col.server_default
        if sd is None:
            return None
        # FetchedValue（如 now() 函式）→ 改為 CURRENT_TIMESTAMP
        if not hasattr(sd, "arg"):
            return "CURRENT_TIMESTAMP"
        val = sd.arg
        # 非字串（如 sa.func.now() 這類 SA ClauseElement）→ CURRENT_TIMESTAMP
        if not isinstance(val, str):
            return "CURRENT_TIMESTAMP"
        # 字串型 default（如 '0', 'active', '{}'）
        # {} 是 JSON default，SQLite 不支援作為 DEFAULT，略過
        if val.strip() == "{}":
            return None
        # 含 () 表示是函式呼叫（如 now()），改為 CURRENT_TIMESTAMP
        if "(" in val:
            return "CURRENT_TIMESTAMP"
        return val

    def create_table(self, table_name, *columns, **kwargs):
        # 用原始 SQL 建表，避免 SA MetaData FK 解析失敗（跨表 FK 在 test stub 不需強制）
        # 只保留 Column，忽略 CheckConstraint 等 SA constraint 物件
        cols = [c for c in columns if isinstance(c, sa.Column)]
        col_defs = []
        for col in cols:
            col_type = col.type.compile(dialect=self.bind.dialect)
            parts = [f"{col.name} {col_type}"]
            if col.primary_key:
                parts.append("PRIMARY KEY")
            if not col.nullable and not col.primary_key:
                parts.append("NOT NULL")
            sqlite_default = self._sqlite_server_default(col)
            if sqlite_default is not None:
                parts.append(f"DEFAULT {sqlite_default}")
            if col.unique and not col.primary_key:
                parts.append("UNIQUE")
            col_defs.append(" ".join(parts))
        ddl = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(col_defs)})"
        self.bind.execute(text(ddl))

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
        # partial index — SQLite 支援 partial index
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
        self.bind.execute(
            text(
                f"ALTER TABLE {table_name} ADD COLUMN "
                f"{column.name} {col_type}{server_default}{nullable}"
            )
        )

    def drop_column(self, table_name, column_name):
        self.bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    def drop_index(self, index_name, table_name=None, **kwargs):
        self.bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))

    def drop_table(self, table_name):
        self.bind.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    def execute(self, stmt, *args, **kwargs):
        return self.bind.execute(stmt, *args, **kwargs)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("compexpr01", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prerequisite_tables(bind):
    """建立 migration 需要的前置表。"""
    meta = MetaData()
    Table(
        "employees",
        meta,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("hire_date", Date, nullable=True),
    )
    Table(
        "salary_records",
        meta,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer),
        Column("salary_year", Integer),
        Column("salary_month", Integer),
    )
    Table(
        "overtime_records",
        meta,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer),
        Column("overtime_date", Date, nullable=False),
        Column("hours", Float, default=0),
        Column("use_comp_leave", Boolean, default=False),
        Column("comp_leave_granted", Boolean, default=False),
        Column("is_approved", Boolean, nullable=True),
    )
    Table(
        "leave_quotas",
        meta,
        Column("id", Integer, primary_key=True),
        Column("employee_id", Integer),
        Column("leave_type", String(30)),
        Column("total_days", Float, default=0),
    )
    meta.create_all(bind)


def test_compexpr01_upgrade_creates_tables_and_columns(tmp_path):
    """schema 結構驗：兩張新表 + leave_quotas 兩新欄"""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        insp = inspect(conn)
        tables = insp.get_table_names()
        assert "unused_leave_payout_log" in tables, "unused_leave_payout_log 表未建立"
        assert (
            "overtime_comp_leave_grants" in tables
        ), "overtime_comp_leave_grants 表未建立"

        # leave_quotas 要有 period_start / period_end
        lq_cols = {c["name"] for c in insp.get_columns("leave_quotas")}
        assert "period_start" in lq_cols, "leave_quotas.period_start 未新增"
        assert "period_end" in lq_cols, "leave_quotas.period_end 未新增"

        # unused_leave_payout_log 必要欄
        payout_cols = {c["name"] for c in insp.get_columns("unused_leave_payout_log")}
        expected_payout_cols = {
            "id",
            "employee_id",
            "source_type",
            "source_ref_id",
            "hours",
            "hourly_wage",
            "amount",
            "wage_basis_date",
            "salary_record_id",
            "salary_period_year",
            "salary_period_month",
            "meta",
            "created_at",
        }
        assert expected_payout_cols.issubset(
            payout_cols
        ), f"unused_leave_payout_log 缺欄: {expected_payout_cols - payout_cols}"

        # overtime_comp_leave_grants 必要欄
        grant_cols = {c["name"] for c in insp.get_columns("overtime_comp_leave_grants")}
        expected_grant_cols = {
            "id",
            "overtime_record_id",
            "employee_id",
            "granted_hours",
            "granted_at",
            "expires_at",
            "consumed_hours",
            "status",
            "expired_at",
            "payout_salary_record_id",
            "payout_log_id",
            "created_at",
            "updated_at",
        }
        assert expected_grant_cols.issubset(
            grant_cols
        ), f"overtime_comp_leave_grants 缺欄: {expected_grant_cols - grant_cols}"

    engine.dispose()


def test_compexpr01_backfill_existing_ot_to_grants(tmp_path, monkeypatch):
    """既有 OT (use_comp_leave=1, comp_leave_granted=1, is_approved=1) backfill 為 grant row。

    SQLite 不用 TRUE literal，改用整數 1。
    """
    monkeypatch.setenv("LEAVE_BACKFILL_GRACE_MONTHS", "3")

    db_path = tmp_path / "backfill_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)

        # 插入員工
        conn.execute(
            text(
                "INSERT INTO employees (id, name, hire_date) VALUES (1, '測試員工', '2020-03-15')"
            )
        )

        # 插入符合 backfill 條件的 OT
        conn.execute(
            text(
                "INSERT INTO overtime_records (id, employee_id, overtime_date, hours, "
                "use_comp_leave, comp_leave_granted, is_approved) "
                "VALUES (10, 1, '2024-06-01', 4.0, 1, 1, 1)"
            )
        )

        # 插入不符合條件的 OT（is_approved=0）
        conn.execute(
            text(
                "INSERT INTO overtime_records (id, employee_id, overtime_date, hours, "
                "use_comp_leave, comp_leave_granted, is_approved) "
                "VALUES (11, 1, '2024-07-01', 3.0, 1, 1, 0)"
            )
        )

        # 插入不符合條件的 OT（use_comp_leave=0）
        conn.execute(
            text(
                "INSERT INTO overtime_records (id, employee_id, overtime_date, hours, "
                "use_comp_leave, comp_leave_granted, is_approved) "
                "VALUES (12, 1, '2024-08-01', 2.0, 0, 0, 1)"
            )
        )

        # 執行 upgrade（migration 內 backfill SQL 用 TRUE，但 SQLite 接受 TRUE）
        stub = _AlembicOpStub(conn)
        module.op = stub
        module.upgrade()

        # 驗 grant row 存在，且只有 1 筆（overtime_record_id=10）
        result = conn.execute(
            text(
                "SELECT overtime_record_id, granted_hours, expires_at, status "
                "FROM overtime_comp_leave_grants"
            )
        ).fetchall()

        assert len(result) == 1, f"預期 1 筆 grant，實際 {len(result)} 筆"
        row = result[0]
        assert row[0] == 10, f"overtime_record_id 應為 10，實為 {row[0]}"
        assert row[1] == 4.0, f"granted_hours 應為 4.0，實為 {row[1]}"
        assert row[3] == "active", f"status 應為 'active'，實為 {row[3]}"

        # expires_at ≈ today + 90 天（BACKFILL_GRACE_MONTHS=3 → 3*30=90）
        today = date.today()
        expected_expires = today + timedelta(days=3 * 30)
        actual_expires_str = row[2]
        if actual_expires_str:
            actual_expires = date.fromisoformat(str(actual_expires_str))
            assert (
                actual_expires == expected_expires
            ), f"expires_at 應為 {expected_expires}，實為 {actual_expires}"

    engine.dispose()


def test_compexpr01_backfill_leave_quota_period(tmp_path):
    """既有 annual LeaveQuota 應 backfill period_start / period_end。"""
    db_path = tmp_path / "quota_backfill_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)

        # hire_date 2020-03-15，今天 2026-05-26
        # years_elapsed = 2026 - 2020 = 6；(05,26) >= (03,15) → 6
        # period_start = 2020-03-15 + 6y = 2026-03-15
        # period_end = 2027-03-15
        conn.execute(
            text(
                "INSERT INTO employees (id, name, hire_date) VALUES (2, '員工A', '2020-03-15')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO leave_quotas (id, employee_id, leave_type, total_days) "
                "VALUES (100, 2, 'annual', 14.0)"
            )
        )

        stub = _AlembicOpStub(conn)
        module.op = stub
        module.upgrade()

        row = conn.execute(
            text("SELECT period_start, period_end FROM leave_quotas WHERE id = 100")
        ).fetchone()

        assert row is not None
        # period_start should not be NULL
        assert row[0] is not None, "期望 period_start 被 backfill，實為 NULL"
        # period_start should be the most recent anniversary on or before today
        today = date.today()
        ps = date.fromisoformat(str(row[0]))
        pe = date.fromisoformat(str(row[1]))
        # period_start <= today
        assert ps <= today, f"period_start {ps} 應 <= 今天 {today}"
        # period_end = period_start + 1 year (approx)
        # Allow some tolerance for leap year edge cases
        assert pe > ps, "period_end 應 > period_start"
        diff_days = (pe - ps).days
        assert (
            364 <= diff_days <= 366
        ), f"period_end - period_start 應約 1 年，實為 {diff_days} 天"

    engine.dispose()


def test_compexpr01_downgrade_drops_cleanly(tmp_path):
    """downgrade 還原 schema（不嘗試 reverse backfill）"""
    db_path = tmp_path / "downgrade_test.db"
    db_url = f"sqlite:///{db_path}"

    # Step 1: upgrade
    engine = create_engine(db_url)
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prerequisite_tables(conn)
        stub = _AlembicOpStub(conn)
        module.op = stub
        module.upgrade()

        insp = inspect(conn)
        assert "unused_leave_payout_log" in insp.get_table_names()
        assert "overtime_comp_leave_grants" in insp.get_table_names()
        lq_cols = {c["name"] for c in insp.get_columns("leave_quotas")}
        assert "period_start" in lq_cols
        assert "period_end" in lq_cols

    engine.dispose()

    # Step 2: downgrade（新 engine + transaction，SQLite DDL 隔離）
    engine2 = create_engine(db_url)
    module2 = _load_migration_module()

    with engine2.begin() as conn2:
        stub2 = _AlembicOpStub(conn2)
        module2.op = stub2
        module2.downgrade()

        insp2 = inspect(conn2)
        tables = insp2.get_table_names()
        assert (
            "unused_leave_payout_log" not in tables
        ), "downgrade 後 unused_leave_payout_log 應被刪除"
        assert (
            "overtime_comp_leave_grants" not in tables
        ), "downgrade 後 overtime_comp_leave_grants 應被刪除"

        # leave_quotas 欄應移除
        lq_cols2 = {c["name"] for c in insp2.get_columns("leave_quotas")}
        assert "period_start" not in lq_cols2, "downgrade 後 period_start 應被移除"
        assert "period_end" not in lq_cols2, "downgrade 後 period_end 應被移除"

    engine2.dispose()
