"""recruitment geocoding consent + DROP cache

Revision ID: rcrgeoconsent01
Revises: intghealth01
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa

revision = "rcrgeoconsent01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 加 consent 欄位（RecruitmentVisit + RecruitmentIvykidsRecord）
    op.add_column(
        "recruitment_visits",
        sa.Column("geocoding_consent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "recruitment_ivykids_records",
        sa.Column("geocoding_consent_at", sa.DateTime(), nullable=True),
    )

    # 2. RecruitmentVisit grandfather：既有 row 視為已同意（避 heatmap blank-out）
    op.execute(
        "UPDATE recruitment_visits "
        "SET geocoding_consent_at = created_at "
        "WHERE geocoding_consent_at IS NULL"
    )
    # 3. RecruitmentIvykidsRecord 不 grandfather（來源無 consent 證據，留 NULL → 不上 heatmap）

    # 4. 清空 cache（下次 sync 會以 truncated key 重灌；operational note: ~200 Google API call）
    op.execute("DELETE FROM recruitment_geocode_cache")


def downgrade() -> None:
    op.drop_column("recruitment_visits", "geocoding_consent_at")
    op.drop_column("recruitment_ivykids_records", "geocoding_consent_at")
    # 注意：cache DELETE 不 reversible（無 backup）；downgrade 後 cache 仍空，下次 sync 重灌
