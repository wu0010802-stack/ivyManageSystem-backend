"""bonus_configs.late_deduction_per_time server_default 100 → 50（對齊 model / Excel）

Revision ID: latededu01
Revises: allergyenc01
Create Date: 2026-06-05

說明（QA 2026-06-04 P2-1）：
bonuscfg_p2 為 late_deduction_per_time 設 server_default="100"，但 ORM model
（models/config.py）default=50、Excel 遲到一覽表規則為 -50/次（業主 2026-06-02 確認 50）。
divergence 後果：被 server_default 回填的既有列、或任何非 ORM（raw SQL）INSERT 的列
會落 100；runtime services/year_end/auto_derive/attendance_deductions._rate 只在 NULL
才 fallback 50，讀到 100 即直接用 → 年終遲到罰則 2× 超扣。

修正：
  1. server_default 改 "50"，與 model / Excel 一致（其餘 6 個年終規則 default 皆已對齊）。
  2. 將既有值為 100 的列 UPDATE 回 50（Excel 規則下 100 一律為錯值；無業務情境會合法設 100）。

downgrade 還原 server_default="100"（資料 UPDATE 不可逆，僅還原 default 契約）。
"""

from alembic import op

revision = "latededu01"
down_revision = "allergyenc01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 對齊 server_default 為 50（防未來 server_default 回填 / raw INSERT 落 100）
    op.alter_column(
        "bonus_configs",
        "late_deduction_per_time",
        server_default="50",
    )
    # 2) 修正既有被回填為 100 的列（Excel 規則下 100 為錯值）
    op.execute(
        "UPDATE bonus_configs SET late_deduction_per_time = 50 "
        "WHERE late_deduction_per_time = 100"
    )


def downgrade() -> None:
    # 還原 server_default（資料 UPDATE 不可逆）
    op.alter_column(
        "bonus_configs",
        "late_deduction_per_time",
        server_default="100",
    )
