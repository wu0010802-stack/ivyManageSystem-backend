"""add recruitment market intelligence tables and columns

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-04-10 00:00:00.000000
"""

from alembic import op
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, inspect, text


revision = "h8i9j0k1l2m3"
down_revision = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


_GEOCODE_TABLE = "recruitment_geocode_cache"
_GEOCODE_COLS = [
    ("matched_address", "VARCHAR(255)"),
    ("town_code", "VARCHAR(20)"),
    ("town_name", "VARCHAR(50)"),
    ("county_name", "VARCHAR(50)"),
    ("land_use_label", "VARCHAR(120)"),
    ("travel_minutes", "FLOAT"),
    ("travel_distance_km", "FLOAT"),
    ("data_quality", "VARCHAR(20) DEFAULT 'partial' NOT NULL"),
]


def _add_column_if_missing(bind, table_name: str, column_name: str, ddl: str) -> None:
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name not in cols:
        bind.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _GEOCODE_TABLE in inspector.get_table_names():
        for column_name, ddl in _GEOCODE_COLS:
            _add_column_if_missing(bind, _GEOCODE_TABLE, column_name, ddl)

        existing_indexes = {idx["name"] for idx in inspector.get_indexes(_GEOCODE_TABLE)}
        if "ix_recruitment_geocode_cache_town_code" not in existing_indexes:
            op.create_index("ix_recruitment_geocode_cache_town_code", _GEOCODE_TABLE, ["town_code"])

    if "recruitment_campus_settings" not in inspector.get_table_names():
        op.create_table(
            "recruitment_campus_settings",
            Column("id", Integer, primary_key=True),
            Column("campus_name", String(100), nullable=False, server_default="本園"),
            Column("campus_address", String(255), nullable=False, server_default=""),
            Column("campus_lat", Float, nullable=True),
            Column("campus_lng", Float, nullable=True),
            Column("travel_mode", String(20), nullable=False, server_default="driving"),
            Column("created_at", DateTime, nullable=True),
            Column("updated_at", DateTime, nullable=True),
        )

    if "recruitment_area_insight_cache" not in inspector.get_table_names():
        op.create_table(
            "recruitment_area_insight_cache",
            Column("id", Integer, primary_key=True),
            Column("county_name", String(50), nullable=True),
            Column("district", String(50), nullable=False),
            Column("town_code", String(20), nullable=True),
            Column("population_density", Float, nullable=True),
            Column("population_0_6", Integer, nullable=True),
            Column("data_completeness", String(20), nullable=False, server_default="partial"),
            Column("source_notes", Text, nullable=True),
            Column("synced_at", DateTime, nullable=True),
            Column("created_at", DateTime, nullable=True),
            Column("updated_at", DateTime, nullable=True),
        )
        op.create_index("ix_recruitment_area_insight_cache_district", "recruitment_area_insight_cache", ["district"])
        op.create_index("ix_recruitment_area_insight_cache_town_code", "recruitment_area_insight_cache", ["town_code"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "recruitment_area_insight_cache" in inspector.get_table_names():
        op.drop_index("ix_recruitment_area_insight_cache_town_code", table_name="recruitment_area_insight_cache")
        op.drop_index("ix_recruitment_area_insight_cache_district", table_name="recruitment_area_insight_cache")
        op.drop_table("recruitment_area_insight_cache")

    if "recruitment_campus_settings" in inspector.get_table_names():
        op.drop_table("recruitment_campus_settings")

    if _GEOCODE_TABLE in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(_GEOCODE_TABLE)}
        if "ix_recruitment_geocode_cache_town_code" in existing_indexes:
            op.drop_index("ix_recruitment_geocode_cache_town_code", table_name=_GEOCODE_TABLE)

        existing_cols = {c["name"] for c in inspector.get_columns(_GEOCODE_TABLE)}
        for column_name, _ddl in reversed(_GEOCODE_COLS):
            if column_name in existing_cols:
                bind.execute(text(f"ALTER TABLE {_GEOCODE_TABLE} DROP COLUMN {column_name}"))
