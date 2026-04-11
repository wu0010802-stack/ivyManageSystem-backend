"""add google place id to recruitment geocode cache

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-04-10 00:30:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "i9j0k1l2m3n4"
down_revision = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_geocode_cache" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("recruitment_geocode_cache")}
    if "google_place_id" not in columns:
        bind.execute(text("ALTER TABLE recruitment_geocode_cache ADD COLUMN google_place_id VARCHAR(128)"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_geocode_cache" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("recruitment_geocode_cache")}
    if "google_place_id" in columns:
        bind.execute(text("ALTER TABLE recruitment_geocode_cache DROP COLUMN google_place_id"))
