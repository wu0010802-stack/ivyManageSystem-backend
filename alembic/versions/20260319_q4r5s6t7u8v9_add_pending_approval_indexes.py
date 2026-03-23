"""add composite indexes for pending approval queries on leave/overtime

Revision ID: q4r5s6t7u8v9
Revises: p3q4r5s6t7u8
Create Date: 2026-03-19

"""
from alembic import op

revision = 'q4r5s6t7u8v9'
down_revision = 'p3q4r5s6t7u8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 通知彙總、審核清單常見查詢：WHERE is_approved IS NULL AND start_date BETWEEN ...
    op.create_index('ix_leave_approved_start_date', 'leave_records', ['is_approved', 'start_date'], unique=False)
    op.create_index('ix_overtime_approved_date', 'overtime_records', ['is_approved', 'overtime_date'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_overtime_approved_date', table_name='overtime_records')
    op.drop_index('ix_leave_approved_start_date', table_name='leave_records')
