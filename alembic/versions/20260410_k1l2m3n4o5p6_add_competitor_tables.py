"""add competitor tables

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-04-10 23:00:00.000000

NOTE: This file was recreated as a stub after the original was accidentally deleted.
      The tables were already applied to the database before the file was lost.
      upgrade() is idempotent — skips creation if tables already exist.
"""

from alembic import op
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Double, Float, Integer,
    JSON, String, Text, inspect, text,
)


revision = "k1l2m3n4o5p6"
down_revision = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = inspector.get_table_names()

    if "recruitment_competitors" not in existing:
        op.execute(text("""
            CREATE TABLE recruitment_competitors (
                id SERIAL PRIMARY KEY,
                source_key VARCHAR(120) NOT NULL UNIQUE,
                source_name VARCHAR(50) NOT NULL,
                school_code VARCHAR(40),
                school_name VARCHAR(150) NOT NULL,
                ownership VARCHAR(40),
                county_name VARCHAR(50),
                district VARCHAR(50),
                town_code VARCHAR(20),
                address VARCHAR(255),
                phone VARCHAR(100),
                capacity INTEGER,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                matched_address VARCHAR(255),
                travel_minutes DOUBLE PRECISION,
                travel_distance_km DOUBLE PRECISION,
                data_quality VARCHAR(20) NOT NULL,
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP
            )
        """))
        op.execute(text("CREATE INDEX ix_recruitment_competitors_county_name ON recruitment_competitors (county_name)"))
        op.execute(text("CREATE INDEX ix_recruitment_competitors_district ON recruitment_competitors (district)"))
        op.execute(text("CREATE INDEX ix_recruitment_competitors_id ON recruitment_competitors (id)"))
        op.execute(text("CREATE INDEX ix_recruitment_competitors_school_code ON recruitment_competitors (school_code)"))
        op.execute(text("CREATE INDEX ix_recruitment_competitors_town_code ON recruitment_competitors (town_code)"))

    if "competitor_school" not in existing:
        op.execute(text("""
            CREATE TABLE competitor_school (
                id SERIAL PRIMARY KEY,
                source_school_id VARCHAR(100) NOT NULL UNIQUE,
                school_name VARCHAR(255) NOT NULL,
                owner_name VARCHAR(255),
                school_type VARCHAR(50),
                pre_public_type VARCHAR(50),
                is_active BOOLEAN NOT NULL DEFAULT true,
                phone VARCHAR(50),
                website VARCHAR(500),
                city VARCHAR(50),
                district VARCHAR(50),
                address VARCHAR(500),
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                approved_capacity INTEGER,
                monthly_fee INTEGER,
                has_penalty BOOLEAN NOT NULL DEFAULT false,
                source_updated_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """))
        op.execute(text("CREATE INDEX idx_competitor_city_district ON competitor_school (city, district)"))
        op.execute(text("CREATE INDEX ix_competitor_school_has_penalty ON competitor_school (has_penalty)"))
        op.execute(text("CREATE INDEX ix_competitor_school_is_active ON competitor_school (is_active)"))
        op.execute(text("CREATE INDEX ix_competitor_school_monthly_fee ON competitor_school (monthly_fee)"))
        op.execute(text("CREATE INDEX ix_competitor_school_school_name ON competitor_school (school_name)"))
        op.execute(text("CREATE INDEX ix_competitor_school_school_type ON competitor_school (school_type)"))

    if "competitor_tag" not in existing:
        op.execute(text("""
            CREATE TABLE competitor_tag (
                id SERIAL PRIMARY KEY,
                school_id INTEGER NOT NULL REFERENCES competitor_school(id) ON DELETE CASCADE,
                tag_code VARCHAR(50) NOT NULL,
                tag_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_competitor_tag_school_code UNIQUE (school_id, tag_code)
            )
        """))
        op.execute(text("CREATE INDEX ix_competitor_tag_school_id ON competitor_tag (school_id)"))
        op.execute(text("CREATE INDEX ix_competitor_tag_tag_code ON competitor_tag (tag_code)"))

    if "competitor_note" not in existing:
        op.execute(text("""
            CREATE TABLE competitor_note (
                id SERIAL PRIMARY KEY,
                school_id INTEGER NOT NULL REFERENCES competitor_school(id) ON DELETE CASCADE,
                note_type VARCHAR(50) NOT NULL,
                note_content TEXT NOT NULL,
                priority_level INTEGER NOT NULL DEFAULT 0,
                created_by VARCHAR(100),
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """))
        op.execute(text("CREATE INDEX ix_competitor_note_note_type ON competitor_note (note_type)"))
        op.execute(text("CREATE INDEX ix_competitor_note_school_id ON competitor_note (school_id)"))

    if "competitor_penalty" not in existing:
        op.execute(text("""
            CREATE TABLE competitor_penalty (
                id SERIAL PRIMARY KEY,
                school_id INTEGER NOT NULL REFERENCES competitor_school(id) ON DELETE CASCADE,
                penalty_date DATE,
                law_name VARCHAR(255),
                penalty_reason TEXT,
                penalty_result TEXT,
                penalty_amount VARCHAR(100),
                source_penalty_id VARCHAR(100) NOT NULL UNIQUE,
                raw_data JSON,
                created_at TIMESTAMP NOT NULL
            )
        """))
        op.execute(text("CREATE INDEX ix_competitor_penalty_penalty_date ON competitor_penalty (penalty_date)"))
        op.execute(text("CREATE INDEX ix_competitor_penalty_school_id ON competitor_penalty (school_id)"))

    if "competitor_change_log" not in existing:
        op.execute(text("""
            CREATE TABLE competitor_change_log (
                id SERIAL PRIMARY KEY,
                school_id INTEGER NOT NULL REFERENCES competitor_school(id) ON DELETE CASCADE,
                field_name VARCHAR(100) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at TIMESTAMP NOT NULL
            )
        """))
        op.execute(text("CREATE INDEX ix_competitor_change_log_changed_at ON competitor_change_log (changed_at)"))
        op.execute(text("CREATE INDEX ix_competitor_change_log_field_name ON competitor_change_log (field_name)"))
        op.execute(text("CREATE INDEX ix_competitor_change_log_school_id ON competitor_change_log (school_id)"))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS competitor_change_log CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS competitor_penalty CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS competitor_note CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS competitor_tag CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS competitor_school CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS recruitment_competitors CASCADE"))
