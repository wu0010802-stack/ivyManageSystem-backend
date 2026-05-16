"""擴增 fee_templates CHECK 至 9 種 fee_type

Revision ID: f33ty9types
Revises: adj1stmnt001
Create Date: 2026-05-16

commit 4e9ff6c5 已把 Pydantic FeeTemplateCreate / GenerateFromTemplatesRequest 放行
9 種 fee_type（新增 tuition / transport / summer_uniform / summer_sports），
但 alembic 20260514_tu1ti0nr3f4ct 的 CHECK 與 models/fees.py CheckConstraint 仍卡 5 種，
建立或產生新類型範本時 PostgreSQL 直接 500。本 migration 把 CHECK 對齊 9 種。
"""

from typing import Sequence, Union

from sqlalchemy import inspect

from alembic import op

revision: str = "f33ty9types"
down_revision: Union[str, Sequence[str], None] = "adj1stmnt001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "fee_templates" not in tables:
        return
    op.drop_constraint("ck_fee_template_type", "fee_templates", type_="check")
    op.create_check_constraint(
        "ck_fee_template_type",
        "fee_templates",
        "fee_type IN ("
        "'registration','miscellaneous','monthly','material','insurance',"
        "'tuition','transport','summer_uniform','summer_sports'"
        ")",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "fee_templates" not in tables:
        return
    op.drop_constraint("ck_fee_template_type", "fee_templates", type_="check")
    op.create_check_constraint(
        "ck_fee_template_type",
        "fee_templates",
        "fee_type IN ('registration','miscellaneous','monthly','material','insurance')",
    )
