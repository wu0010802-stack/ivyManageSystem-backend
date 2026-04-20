"""create employee_educations / certificates / contracts tables

員工詳情擴充：學歷 / 證照 / 合約（不含附件上傳）。
FK 使用 ondelete="CASCADE"，員工被 hard delete 時連動刪除。

Revision ID: i7d8e9f0a1b2
Revises: h6c7d8e9f0a1
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "i7d8e9f0a1b2"
down_revision = "h6c7d8e9f0a1"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "employee_educations" not in tables:
        op.create_table(
            "employee_educations",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "employee_id",
                sa.Integer,
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("school_name", sa.String(100), nullable=False),
            sa.Column("major", sa.String(100), nullable=True),
            sa.Column("degree", sa.String(20), nullable=False),
            sa.Column("graduation_date", sa.Date(), nullable=True),
            sa.Column(
                "is_highest",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("remark", sa.String(255), nullable=True),
            sa.Column(
                "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
        )
        op.create_index("ix_emp_edu_employee", "employee_educations", ["employee_id"])

    if "employee_certificates" not in tables:
        op.create_table(
            "employee_certificates",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "employee_id",
                sa.Integer,
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("certificate_name", sa.String(100), nullable=False),
            sa.Column("issuer", sa.String(100), nullable=True),
            sa.Column("certificate_number", sa.String(100), nullable=True),
            sa.Column("issued_date", sa.Date(), nullable=True),
            sa.Column("expiry_date", sa.Date(), nullable=True),
            sa.Column("remark", sa.String(255), nullable=True),
            sa.Column(
                "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
        )
        op.create_index(
            "ix_emp_cert_employee", "employee_certificates", ["employee_id"]
        )
        op.create_index("ix_emp_cert_expiry", "employee_certificates", ["expiry_date"])

    if "employee_contracts" not in tables:
        op.create_table(
            "employee_contracts",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "employee_id",
                sa.Integer,
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("contract_type", sa.String(20), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("salary_at_contract", sa.Float, nullable=True),
            sa.Column("remark", sa.String(255), nullable=True),
            sa.Column(
                "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
            ),
        )
        op.create_index(
            "ix_emp_contract_employee", "employee_contracts", ["employee_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    for tbl, idxs in [
        ("employee_contracts", ["ix_emp_contract_employee"]),
        (
            "employee_certificates",
            ["ix_emp_cert_expiry", "ix_emp_cert_employee"],
        ),
        ("employee_educations", ["ix_emp_edu_employee"]),
    ]:
        if tbl in tables:
            existing_idx = {ix["name"] for ix in inspector.get_indexes(tbl)}
            for ix in idxs:
                if ix in existing_idx:
                    op.drop_index(ix, table_name=tbl)
            op.drop_table(tbl)
