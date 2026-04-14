"""官網報名拆表 migration 回歸測試。"""

import importlib.util
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
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
    / "20260413_o6p7q8r9s0t1_remove_external_sync_fields.py"
)


class _AlembicOpStub:
    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind

    def create_table(self, table_name, *columns):
        metadata = MetaData()
        Table(table_name, metadata, *columns)
        metadata.create_all(self.bind)

    def create_index(self, index_name, table_name, columns, unique=False):
        metadata = MetaData()
        table = Table(table_name, metadata, autoload_with=self.bind)
        Index(index_name, *(table.c[column] for column in columns), unique=unique).create(self.bind)

    def drop_index(self, index_name, table_name=None):
        self.bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))

    def drop_table(self, table_name):
        self.bind.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("split_ivykids_recruitment", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_legacy_tables(bind):
    metadata = MetaData()
    visits = Table(
        "recruitment_visits",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("month", String(10), nullable=False),
        Column("visit_date", String(50), nullable=True),
        Column("child_name", String(50), nullable=False),
        Column("birthday", Date, nullable=True),
        Column("grade", String(20), nullable=True),
        Column("phone", String(100), nullable=True),
        Column("address", String(200), nullable=True),
        Column("district", String(30), nullable=True),
        Column("source", String(50), nullable=True),
        Column("referrer", String(50), nullable=True),
        Column("deposit_collector", String(50), nullable=True),
        Column("has_deposit", Boolean, nullable=False, server_default=text("0")),
        Column("notes", Text, nullable=True),
        Column("parent_response", Text, nullable=True),
        Column("no_deposit_reason", String(100), nullable=True),
        Column("no_deposit_reason_detail", Text, nullable=True),
        Column("enrolled", Boolean, nullable=False, server_default=text("0")),
        Column("transfer_term", Boolean, nullable=False, server_default=text("0")),
        Column("expected_start_label", String(50), nullable=True),
        Column("created_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=True),
        Column("external_source", String(50), nullable=True),
        Column("external_id", String(100), nullable=True),
        Column("external_status", String(50), nullable=True),
        Column("external_created_at", String(50), nullable=True),
    )
    sync_states = Table(
        "recruitment_sync_states",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("provider_name", String(50), nullable=False),
        Column("provider_label", String(50), nullable=True),
    )
    metadata.create_all(bind)

    Index("ix_recruitment_visits_external_source", visits.c.external_source).create(bind)
    Index("ix_recruitment_visits_external_id", visits.c.external_id).create(bind)
    Index(
        "ux_rv_external_source_id",
        visits.c.external_source,
        visits.c.external_id,
        unique=True,
    ).create(bind)

    bind.execute(
        visits.insert(),
        {
            "id": 1,
            "month": "115.04",
            "visit_date": "115.04.11",
            "child_name": "手動名單",
            "phone": "0912000111",
            "source": "朋友介紹",
            "has_deposit": False,
            "enrolled": False,
            "transfer_term": False,
            "created_at": datetime(2026, 4, 11, 8, 0, 0),
            "updated_at": datetime(2026, 4, 11, 8, 0, 0),
        },
    )
    bind.execute(
        visits.insert(),
        {
            "id": 2,
            "month": "115.04",
            "visit_date": "2026-04-12 10:30",
            "child_name": "官網名單",
            "phone": "0912333444",
            "address": "高雄市左營區文學路100號",
            "district": "左營區",
            "source": "官網預約",
            "has_deposit": True,
            "enrolled": False,
            "transfer_term": False,
            "created_at": datetime(2026, 4, 12, 10, 31, 0),
            "updated_at": datetime(2026, 4, 12, 10, 31, 0),
            "external_source": "ivykids_yihua_backend",
            "external_id": "1001",
            "external_status": "預約正常",
            "external_created_at": "2026-04-10 09:15",
        },
    )
    bind.execute(
        sync_states.insert(),
        [
            {
                "id": 1,
                "provider_name": "ivykids_yihua_backend",
                "provider_label": "義華校官網",
            }
        ],
    )


def test_upgrade_moves_legacy_ivykids_rows_to_dedicated_table(tmp_path):
    db_path = tmp_path / "recruitment-ivykids-migration.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as connection:
        _create_legacy_tables(connection)
        module.op = _AlembicOpStub(connection)

        module.upgrade()
        module.upgrade()

        inspector = inspect(connection)
        assert "recruitment_sync_states" in inspector.get_table_names()
        assert "recruitment_ivykids_records" in inspector.get_table_names()

        visit_columns = {column["name"] for column in inspector.get_columns("recruitment_visits")}
        assert "external_source" not in visit_columns
        assert "external_id" not in visit_columns
        assert "external_status" not in visit_columns
        assert "external_created_at" not in visit_columns

        manual_rows = connection.execute(
            text("SELECT month, child_name, source FROM recruitment_visits ORDER BY id")
        ).mappings().all()
        assert manual_rows == [
            {
                "month": "115.04",
                "child_name": "手動名單",
                "source": "朋友介紹",
            }
        ]

        ivykids_rows = connection.execute(
            text(
                """
                SELECT external_id, external_status, external_created_at, month, child_name, source, phone
                FROM recruitment_ivykids_records
                ORDER BY id
                """
            )
        ).mappings().all()
        assert ivykids_rows == [
            {
                "external_id": "1001",
                "external_status": "預約正常",
                "external_created_at": "2026-04-10 09:15",
                "month": "115.04",
                "child_name": "官網名單",
                "source": "官網預約",
                "phone": "0912333444",
            }
        ]

    engine.dispose()
