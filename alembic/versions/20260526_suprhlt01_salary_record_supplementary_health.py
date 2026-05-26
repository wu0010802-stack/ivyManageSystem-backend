"""salary_records 加 supplementary_health_employee column

Revision ID: suprhlt01
Revises: mergeheads04
Create Date: 2026-05-26

二代健保補充保費（員工自付）拆出 health_insurance_employee 獨立持久化。
- 兼職薪資路徑（engine.py:1567，hourly 月累計 ≥ 29500）原本只在 breakdown 內，DB 沒落
- 獎金路徑（services/salary/supplementary_premium.py，年累計逾 4× 投保薪資）2026-05-26 新增

兩條路徑值都會 += 到 breakdown.supplementary_health_employee，並透過 _fill_salary_record
寫入本欄；同時繼續疊加進 health_insurance_employee（保留 gross 健保欄位口徑不變，
供既有報表 / 政府申報書沿用，避免 silent breaking）。

Money 型別 default 0 nullable=False server_default='0'，對齊 appraisal_year_end_bonus
等獨立欄位 pattern（models/salary.py:242-248）。
PG 11+ add column with non-volatile default 為 metadata-only operation，無鎖風險。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "suprhlt01"
down_revision: Union[str, Sequence[str], None] = "mergeheads04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "salary_records",
        sa.Column(
            "supplementary_health_employee",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
            comment="二代健保補充保費（員工自付；hourly 兼職月累計 + 獎金年累計逾 4× 投保薪資）",
        ),
    )


def downgrade() -> None:
    op.drop_column("salary_records", "supplementary_health_employee")
