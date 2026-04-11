"""add recruitment external sync fields

新增 recruitment_visits 的外部同步欄位，供義華校官網後台資料同步使用。

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-04-12 00:30:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "l2m3n4o5p6q7"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None

_TABLE = "recruitment_visits"
_COLUMN_DEFS = {
    "external_source": "VARCHAR(50)",
    "external_id": "VARCHAR(100)",
    "external_status": "VARCHAR(50)",
}
_INDEXES = {
    "ix_recruitment_visits_external_source": ["external_source"],
    "ix_recruitment_visits_external_id": ["external_id"],
    "ux_rv_external_source_id": ["external_source", "external_id"],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, ddl in _COLUMN_DEFS.items():
        if name not in columns:
            bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl}"))

    existing_indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if "ix_recruitment_visits_external_source" not in existing_indexes:
        op.create_index("ix_recruitment_visits_external_source", _TABLE, ["external_source"])
    if "ix_recruitment_visits_external_id" not in existing_indexes:
        op.create_index("ix_recruitment_visits_external_id", _TABLE, ["external_id"])
    if "ux_rv_external_source_id" not in existing_indexes:
        op.create_index("ux_rv_external_source_id", _TABLE, ["external_source", "external_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    for name in ("ux_rv_external_source_id", "ix_recruitment_visits_external_id", "ix_recruitment_visits_external_source"):
        if name in existing_indexes:
            op.drop_index(name, table_name=_TABLE)

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name in ("external_status", "external_id", "external_source"):
        if name in columns:
            bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {name}"))
