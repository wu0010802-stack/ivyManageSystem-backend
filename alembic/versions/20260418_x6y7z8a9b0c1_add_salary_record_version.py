"""add version column to salary_records for optimistic locking

新增 salary_records.version 欄位用於樂觀鎖，防止多人同時編輯同筆薪資時
後寫入者靜默覆蓋前一筆調整（造成資料遺失）。

API 端點透過 If-Match header 比對版本，若不符則返回 409 Conflict。

Revision ID: x6y7z8a9b0c1
Revises: w5x6y7z8a9b0
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "x6y7z8a9b0c1"
down_revision = "w5x6y7z8a9b0"
branch_labels = None
depends_on = None


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "salary_records" not in inspect(bind).get_table_names():
        return

    cols = _existing_cols(bind, "salary_records")
    if "version" not in cols:
        op.add_column(
            "salary_records",
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "salary_records" not in inspect(bind).get_table_names():
        return

    cols = _existing_cols(bind, "salary_records")
    if "version" in cols:
        op.drop_column("salary_records", "version")
