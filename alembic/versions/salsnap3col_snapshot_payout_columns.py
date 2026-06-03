"""salary_snapshots：補 3 個獨立轉帳/拆分金額欄（與 SalaryRecord 對齊）

Revision ID: salsnap3col
Revises: schedwm01
Create Date: 2026-06-03

SalarySnapshot 原漏 supplementary_health_employee / appraisal_year_end_bonus /
unused_leave_payout 三欄；_copy_record_to_snapshot 依兩表欄位交集反射複製 → 跳過
這三欄 → 稽核快照無法逐項還原歷史薪條的補充保費／考核年終／特休折現。補欄後
反射自動帶入。現有列以 server_default 0 回填（informational，無 backfill 計算）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "salsnap3col"
down_revision: Union[str, Sequence[str], None] = "schedwm01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    "supplementary_health_employee",
    "appraisal_year_end_bonus",
    "unused_leave_payout",
)


def upgrade() -> None:
    for col in _COLUMNS:
        op.add_column(
            "salary_snapshots",
            sa.Column(col, sa.Numeric(12, 2), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for col in reversed(_COLUMNS):
        op.drop_column("salary_snapshots", col)
