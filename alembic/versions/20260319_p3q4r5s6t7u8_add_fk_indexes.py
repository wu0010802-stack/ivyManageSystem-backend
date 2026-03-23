"""add FK indexes for job_title_id, classroom_id, bonus_config_id, attendance_policy_id

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t7
Create Date: 2026-03-19

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'p3q4r5s6t7u8'
down_revision = 'o2p3q4r5s6t7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_employee_job_title_id', 'employees', ['job_title_id'], unique=False)
    op.create_index('ix_employee_classroom_id', 'employees', ['classroom_id'], unique=False)
    op.create_index('ix_salary_bonus_config_id', 'salary_records', ['bonus_config_id'], unique=False)
    op.create_index('ix_salary_attendance_policy_id', 'salary_records', ['attendance_policy_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_salary_attendance_policy_id', table_name='salary_records')
    op.drop_index('ix_salary_bonus_config_id', table_name='salary_records')
    op.drop_index('ix_employee_classroom_id', table_name='employees')
    op.drop_index('ix_employee_job_title_id', table_name='employees')
