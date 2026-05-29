"""data quality reports table

Revision ID: dqreport01
Revises: auditfor01
Create Date: 2026-05-29

Ch2 of observability-forensic-and-design-tokens spec.
新表存放每日 invariant 偵測結果（員工離職未關、學生 lifecycle terminal、
ContactBook 孤兒、Guardian 孤兒、SalaryRecord 孤兒）。
"""

from alembic import op
import sqlalchemy as sa

revision = "dqreport01"
down_revision = "auditfor01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_quality_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(4), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column(
            "ack_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ack_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_dqr_rule_detected",
        "data_quality_reports",
        ["rule_code", "detected_at"],
    )
    op.create_index(
        "ix_dqr_status_severity",
        "data_quality_reports",
        ["status", "severity"],
    )
    # Partial unique index：同 entity 同 rule open 狀態只一筆
    op.execute("""
        CREATE UNIQUE INDEX ix_dqr_dedup_open
        ON data_quality_reports (dedup_key)
        WHERE status = 'open';
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dqr_dedup_open;")
    op.drop_index("ix_dqr_status_severity", table_name="data_quality_reports")
    op.drop_index("ix_dqr_rule_detected", table_name="data_quality_reports")
    op.drop_table("data_quality_reports")
