"""add public display fields to activity_registration_settings

新增前台公開頁可客製化欄位：頁面標題、學期徽章、活動日期、對象、
表單卡片標題、海報 URL。所有欄位皆可為 null，前端會 fallback 至預設字串。

Revision ID: y7z8a9b0c1d2
Revises: x6y7z8a9b0c1
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "y7z8a9b0c1d2"
down_revision = "x6y7z8a9b0c1"
branch_labels = None
depends_on = None


_NEW_COLUMNS = [
    ("page_title", sa.String(length=200)),
    ("term_label", sa.String(length=50)),
    ("event_date_label", sa.String(length=50)),
    ("target_audience", sa.String(length=100)),
    ("form_card_title", sa.String(length=200)),
    ("poster_url", sa.String(length=500)),
]


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "activity_registration_settings" not in inspect(bind).get_table_names():
        return

    cols = _existing_cols(bind, "activity_registration_settings")
    for name, col_type in _NEW_COLUMNS:
        if name not in cols:
            op.add_column(
                "activity_registration_settings",
                sa.Column(name, col_type, nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "activity_registration_settings" not in inspect(bind).get_table_names():
        return

    cols = _existing_cols(bind, "activity_registration_settings")
    for name, _ in _NEW_COLUMNS:
        if name in cols:
            op.drop_column("activity_registration_settings", name)
