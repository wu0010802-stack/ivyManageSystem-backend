"""Portal 教師端 — 聯絡簿範本表

Revision ID: r3s4t5u6v7w8
Revises: p1q2r3s4t5u6
Create Date: 2026-05-02

新增 contact_book_templates：教師個人 / 園所共用聯絡簿範本，
加速批次填寫（套用全班、複製常用評語）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_book_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "scope",
            sa.String(20),
            nullable=False,
            server_default="personal",
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id"),
            nullable=True,
        ),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "scope IN ('personal','shared')",
            name="ck_contact_book_template_scope",
        ),
    )
    op.create_index(
        "ix_contact_book_template_owner",
        "contact_book_templates",
        ["owner_user_id", "is_archived"],
    )
    op.create_index(
        "ix_contact_book_template_shared",
        "contact_book_templates",
        ["scope", "is_archived"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_book_template_shared", table_name="contact_book_templates"
    )
    op.drop_index("ix_contact_book_template_owner", table_name="contact_book_templates")
    op.drop_table("contact_book_templates")
