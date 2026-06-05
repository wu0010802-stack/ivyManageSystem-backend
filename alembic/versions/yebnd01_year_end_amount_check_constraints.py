"""pentest E1：年終金額 DB 層 CheckConstraint（量級守衛）

Revision ID: yebnd01
Revises: allergyenc01
Create Date: 2026-06-05

對 special_bonus_items.amount 與 year_end_settlements.deduction_disciplinary 加
±1,000,000 對稱量級 CheckConstraint，作為繞過 Pydantic schema（如 Excel 匯入
excel_io.py）寫入路徑的 DB 層防禦縱深。保留合法負值（FESTIVAL_DIFF 多退、
disciplinary 獎懲大過），僅擋荒謬注入（pentest 2026-06-05 finding E1）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "yebnd01"
down_revision: Union[str, Sequence[str], None] = "allergyenc01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BOUND = 1_000_000


def upgrade() -> None:
    op.create_check_constraint(
        "ck_special_bonus_item_amount_bound",
        "special_bonus_items",
        f"amount >= -{_BOUND} AND amount <= {_BOUND}",
    )
    op.create_check_constraint(
        "ck_year_end_settlement_disciplinary_bound",
        "year_end_settlements",
        f"deduction_disciplinary >= -{_BOUND} AND deduction_disciplinary <= {_BOUND}",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_year_end_settlement_disciplinary_bound",
        "year_end_settlements",
        type_="check",
    )
    op.drop_constraint(
        "ck_special_bonus_item_amount_bound",
        "special_bonus_items",
        type_="check",
    )
