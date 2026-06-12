"""backfill：attendance_policies / position_salary_configs 的 config_year=0 列

Revision ID: cfgyrfx01
Revises: aprreg01
Create Date: 2026-06-12

Why（設計體檢 2026-06-12 Finding 1）:
    cfgyear01 之後 reader（services/salary/config_resolver）以
    `config_year == 結算年度` 解析，但 writer（PUT /api/config/attendance-policy、
    PUT /api/config/position-salary、startup seed）一直落 model default 0：
    cfgyear01 之後由這些 writer 建立的新版本列 config_year=0，引擎永遠撿不到
    （新值靜默失效）。writer 已同批修正蓋章；本 migration 修復既有 0 值列：
    以 EXTRACT(YEAR FROM created_at) 回填（兩表皆有 created_at；NULL 時蓋當前
    台北年度），idempotent（只動 config_year=0 的列，重跑無 0 值列即 no-op）。

downgrade: no-op —— 資料修復不可逆（原本的 0 值是 bug 產物，不應還原）。
"""

from datetime import datetime
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op

revision: str = "cfgyrfx01"
down_revision: Union[str, Sequence[str], None] = "aprreg01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("attendance_policies", "position_salary_configs")


def _current_taipei_year() -> int:
    return datetime.now(ZoneInfo("Asia/Taipei")).year


def upgrade() -> None:
    bind = op.get_bind()
    year = _current_taipei_year()
    for table in _TABLES:
        if bind.dialect.name == "postgresql":
            op.execute(
                f"UPDATE {table} "
                "SET config_year = EXTRACT(YEAR FROM created_at)::int "
                "WHERE config_year = 0 AND created_at IS NOT NULL"
            )
        else:
            # SQLite（測試）：strftime 取年
            op.execute(
                f"UPDATE {table} "
                "SET config_year = CAST(strftime('%Y', created_at) AS INTEGER) "
                "WHERE config_year = 0 AND created_at IS NOT NULL"
            )
        # created_at 為 NULL 的兜底：蓋當前台北年度
        op.execute(f"UPDATE {table} SET config_year = {year} " "WHERE config_year = 0")


def downgrade() -> None:
    # 資料修復不可逆（config_year=0 是 writer bug 產物），刻意 no-op。
    pass
