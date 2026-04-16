"""add google and kiang fields to competitor_school

Revision ID: u3v4w5x6y7z8
Revises: t2u3v4w5x6y7
Create Date: 2026-04-16
"""

from alembic import op
from sqlalchemy import text, inspect

revision = "u3v4w5x6y7z8"
down_revision = "t2u3v4w5x6y7"
branch_labels = None
depends_on = None

# 需要新增的欄位（名稱, DDL）
NEW_COLUMNS = [
    ("google_place_id", "VARCHAR(255)"),
    ("google_name", "TEXT"),
    ("google_rating", "FLOAT"),
    ("google_rating_count", "INTEGER"),
    ("google_maps_uri", "TEXT"),
    ("google_matched_at", "TIMESTAMP"),
    ("match_confidence", "INTEGER"),
    ("indoor_area_sqm", "FLOAT"),
    ("outdoor_area_sqm", "FLOAT"),
    ("floor_info", "VARCHAR(255)"),
    ("shuttle_info", "VARCHAR(255)"),
    ("has_after_school", "BOOLEAN DEFAULT false"),
    ("kiang_synced_at", "TIMESTAMP"),
]


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("competitor_school")}

    for col_name, col_type in NEW_COLUMNS:
        if col_name not in existing:
            bind.execute(
                text(f"ALTER TABLE competitor_school ADD COLUMN {col_name} {col_type}")
            )

    # 為 google_place_id 加索引（幂等）
    indexes = {idx["name"] for idx in inspector.get_indexes("competitor_school")}
    if "ix_competitor_school_google_place_id" not in indexes and "google_place_id" in {
        c["name"] for c in inspector.get_columns("competitor_school")
    }:
        bind.execute(
            text(
                "CREATE INDEX ix_competitor_school_google_place_id "
                "ON competitor_school (google_place_id)"
            )
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("competitor_school")}

    for col_name, _ in reversed(NEW_COLUMNS):
        if col_name in existing:
            bind.execute(text(f"ALTER TABLE competitor_school DROP COLUMN {col_name}"))
