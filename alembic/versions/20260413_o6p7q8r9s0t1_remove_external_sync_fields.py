"""split ivykids recruitment records into dedicated table

建立獨立的 recruitment_ivykids_records，
將舊 recruitment_visits 中的義華官網同步資料搬移過去後，
再移除 recruitment_visits 的 external_* 欄位。

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
Create Date: 2026-04-13 00:00:00.000000
"""

from alembic import op
from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text, inspect, text


revision = "o6p7q8r9s0t1"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None

_VISITS_TABLE = "recruitment_visits"
_IVYKIDS_TABLE = "recruitment_ivykids_records"
_SOURCE = "ivykids_yihua_backend"
_DROP_INDEXES = (
    "ux_rv_external_source_id",
    "ix_recruitment_visits_external_source",
    "ix_recruitment_visits_external_id",
)
_DROP_COLUMNS = ("external_source", "external_id", "external_status", "external_created_at")


def _column_expr(columns: set[str], name: str) -> str:
    return name if name in columns else "NULL"


def _create_ivykids_table_if_missing(inspector) -> None:
    if _IVYKIDS_TABLE in inspector.get_table_names():
        return

    op.create_table(
        _IVYKIDS_TABLE,
        Column("id", Integer, primary_key=True),
        Column("external_id", String(100), nullable=False),
        Column("external_status", String(50), nullable=True),
        Column("external_created_at", String(50), nullable=True),
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
        Column("notes", Text, nullable=True),
        Column("parent_response", Text, nullable=True),
        Column("has_deposit", Boolean, nullable=False, server_default="0"),
        Column("enrolled", Boolean, nullable=False, server_default="0"),
        Column("transfer_term", Boolean, nullable=False, server_default="0"),
        Column("created_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=True),
    )
    op.create_index(
        "ix_recruitment_ivykids_records_external_id",
        _IVYKIDS_TABLE,
        ["external_id"],
        unique=True,
    )
    op.create_index("ix_recruitment_ivykids_records_month", _IVYKIDS_TABLE, ["month"])
    op.create_index("ix_recruitment_ivykids_records_district", _IVYKIDS_TABLE, ["district"])
    op.create_index("ix_recruitment_ivykids_records_source", _IVYKIDS_TABLE, ["source"])
    op.create_index("ix_recruitment_ivykids_month_source", _IVYKIDS_TABLE, ["month", "source"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    _create_ivykids_table_if_missing(inspector)
    inspector = inspect(bind)

    if _VISITS_TABLE not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns(_VISITS_TABLE)}
    if "external_source" in columns and "external_id" in columns:
        bind.execute(
            text(
                f"""
                INSERT INTO {_IVYKIDS_TABLE} (
                    external_id,
                    external_status,
                    external_created_at,
                    month,
                    visit_date,
                    child_name,
                    birthday,
                    grade,
                    phone,
                    address,
                    district,
                    source,
                    referrer,
                    deposit_collector,
                    notes,
                    parent_response,
                    has_deposit,
                    enrolled,
                    transfer_term,
                    created_at,
                    updated_at
                )
                SELECT
                    external_id,
                    {_column_expr(columns, "external_status")},
                    {_column_expr(columns, "external_created_at")},
                    month,
                    visit_date,
                    child_name,
                    birthday,
                    grade,
                    phone,
                    address,
                    district,
                    source,
                    referrer,
                    deposit_collector,
                    notes,
                    parent_response,
                    has_deposit,
                    enrolled,
                    transfer_term,
                    created_at,
                    updated_at
                FROM {_VISITS_TABLE}
                WHERE external_source = :source
                  AND external_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM {_IVYKIDS_TABLE} ivk
                      WHERE ivk.external_id = {_VISITS_TABLE}.external_id
                  )
                """
            ),
            {"source": _SOURCE},
        )
        bind.execute(
            text(f"DELETE FROM {_VISITS_TABLE} WHERE external_source = :source"),
            {"source": _SOURCE},
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_VISITS_TABLE)}
    for idx_name in _DROP_INDEXES:
        if idx_name in existing_indexes:
            op.drop_index(idx_name, table_name=_VISITS_TABLE)

    columns = {col["name"] for col in inspect(bind).get_columns(_VISITS_TABLE)}
    for column_name in _DROP_COLUMNS:
        if column_name in columns:
            bind.execute(text(f"ALTER TABLE {_VISITS_TABLE} DROP COLUMN {column_name}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _VISITS_TABLE in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns(_VISITS_TABLE)}
        if "external_source" not in columns:
            bind.execute(text(f"ALTER TABLE {_VISITS_TABLE} ADD COLUMN external_source VARCHAR(50)"))
        if "external_id" not in columns:
            bind.execute(text(f"ALTER TABLE {_VISITS_TABLE} ADD COLUMN external_id VARCHAR(100)"))
        if "external_status" not in columns:
            bind.execute(text(f"ALTER TABLE {_VISITS_TABLE} ADD COLUMN external_status VARCHAR(50)"))
        if "external_created_at" not in columns:
            bind.execute(text(f"ALTER TABLE {_VISITS_TABLE} ADD COLUMN external_created_at VARCHAR(50)"))

        existing_indexes = {idx["name"] for idx in inspect(bind).get_indexes(_VISITS_TABLE)}
        if "ix_recruitment_visits_external_source" not in existing_indexes:
            op.create_index("ix_recruitment_visits_external_source", _VISITS_TABLE, ["external_source"])
        if "ix_recruitment_visits_external_id" not in existing_indexes:
            op.create_index("ix_recruitment_visits_external_id", _VISITS_TABLE, ["external_id"])
        if "ux_rv_external_source_id" not in existing_indexes:
            op.create_index("ux_rv_external_source_id", _VISITS_TABLE, ["external_source", "external_id"], unique=True)

    inspector = inspect(bind)
    if _VISITS_TABLE in inspector.get_table_names() and _IVYKIDS_TABLE in inspector.get_table_names():
        bind.execute(
            text(
                f"""
                INSERT INTO {_VISITS_TABLE} (
                    month,
                    visit_date,
                    child_name,
                    birthday,
                    grade,
                    phone,
                    address,
                    district,
                    source,
                    referrer,
                    deposit_collector,
                    has_deposit,
                    notes,
                    parent_response,
                    no_deposit_reason,
                    no_deposit_reason_detail,
                    enrolled,
                    transfer_term,
                    expected_start_label,
                    created_at,
                    updated_at,
                    external_source,
                    external_id,
                    external_status,
                    external_created_at
                )
                SELECT
                    month,
                    visit_date,
                    child_name,
                    birthday,
                    grade,
                    phone,
                    address,
                    district,
                    source,
                    referrer,
                    deposit_collector,
                    has_deposit,
                    notes,
                    parent_response,
                    NULL,
                    NULL,
                    enrolled,
                    transfer_term,
                    NULL,
                    created_at,
                    updated_at,
                    :source,
                    external_id,
                    external_status,
                    external_created_at
                FROM {_IVYKIDS_TABLE}
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM {_VISITS_TABLE} rv
                    WHERE rv.external_id = {_IVYKIDS_TABLE}.external_id
                )
                """
            ),
            {"source": _SOURCE},
        )

    if _IVYKIDS_TABLE in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(_IVYKIDS_TABLE)}
        for idx_name in (
            "ix_recruitment_ivykids_month_source",
            "ix_recruitment_ivykids_records_source",
            "ix_recruitment_ivykids_records_district",
            "ix_recruitment_ivykids_records_month",
            "ix_recruitment_ivykids_records_external_id",
        ):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name=_IVYKIDS_TABLE)
        op.drop_table(_IVYKIDS_TABLE)
