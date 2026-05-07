"""employees 加 bypass_standard_base 旗標（議題 A 選項 3）

讓行政可逐個指定「不要走 PositionSalaryConfig 標準化、用個人 base_salary」，
解決會計帳冊與系統職位標準化的衝突（如林姿妙 46499、孔祥盈 39351 個人加給）。

Why:
- 2026-04-16 業主決議「同職等同薪」→ 系統加入 _resolve_standard_base 強制標準化
- 2026-05 對齊會計 115.04 帳冊發現：個別員工有年資加給，會計實發 ≠ 系統標準化
- 整批改邏輯會推翻既有業務規則（既有測試也鎖定當前行為）
- 加 per-employee 旗標讓兩種需求並存，業主可逐個打勾

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "i4j5k6l7m8n9"
down_revision = "h3i4j5k6l7m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}
    if "bypass_standard_base" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "bypass_standard_base",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment="True=計薪用 emp.base_salary（個人合約值，含年資加給）；False=走 PositionSalaryConfig 職位標準",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}
    if "bypass_standard_base" in cols:
        op.drop_column("employees", "bypass_standard_base")
