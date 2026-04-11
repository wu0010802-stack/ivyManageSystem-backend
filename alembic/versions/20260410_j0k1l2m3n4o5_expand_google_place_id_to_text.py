"""expand google place id to text

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-04-10 22:15:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_geocode_cache" not in inspector.get_table_names():
        return

    columns = {column["name"]: column for column in inspector.get_columns("recruitment_geocode_cache")}
    google_place_id = columns.get("google_place_id")

    if google_place_id is None:
        bind.execute(text("ALTER TABLE recruitment_geocode_cache ADD COLUMN google_place_id TEXT"))
        return

    if bind.dialect.name == "postgresql":
        bind.execute(text("ALTER TABLE recruitment_geocode_cache ALTER COLUMN google_place_id TYPE TEXT"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_geocode_cache" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("recruitment_geocode_cache")}
    if "google_place_id" not in columns:
        return

    if bind.dialect.name == "postgresql":
        bind.execute(
            text(
                "ALTER TABLE recruitment_geocode_cache "
                "ALTER COLUMN google_place_id TYPE VARCHAR(128) "
                "USING LEFT(google_place_id, 128)"
            )
        )
