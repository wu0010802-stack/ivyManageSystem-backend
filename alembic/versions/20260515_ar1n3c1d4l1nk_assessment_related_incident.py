"""add related_incident_id FK to student_assessments

加入評量↔事件 1對多關聯（評量可選擇性引用某個事件作為觀察依據）。
- nullable: 既有評量不必回填
- ON DELETE SET NULL: incident 刪除時保留評量內容，僅斷開引用

Revision ID: ar1n3c1d4l1nk
Revises: dr0pf33it3mid
Create Date: 2026-05-15
"""

from alembic import op
import sqlalchemy as sa

revision = "ar1n3c1d4l1nk"
down_revision = "dr0pf33it3mid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "student_assessments",
        sa.Column("related_incident_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_student_assessments_related_incident",
        "student_assessments",
        "student_incidents",
        ["related_incident_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_student_assessments_related_incident",
        "student_assessments",
        ["related_incident_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_student_assessments_related_incident",
        table_name="student_assessments",
    )
    op.drop_constraint(
        "fk_student_assessments_related_incident",
        "student_assessments",
        type_="foreignkey",
    )
    op.drop_column("student_assessments", "related_incident_id")
