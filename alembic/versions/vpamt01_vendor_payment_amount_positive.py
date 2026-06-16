"""廠商付款金額收緊為 amount > 0（取代 amount >= 0）

Revision ID: vpamt01
Revises: enrdwt01
Create Date: 2026-06-16

P3：vendor_payments.amount 原為 `>= 0`，允許 0 元付款，會產生「有筆數、
金額 0」的稽核雜訊。收緊為 `> 0`，與 Pydantic schema（create/update 皆 gt=0）
對齊，作為繞過 schema 寫入路徑的 DB 層防禦縱深。

寫入路徑已封閉（VendorPaymentCreate / VendorPaymentUpdate 兩處 amount=gt=0，
本模組無 Excel 匯入等旁路），且 dev/prod 現存資料無 0 / 負額列，故約束新增安全。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "vpamt01"
down_revision: Union[str, Sequence[str], None] = "enrdwt01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_vendor_payments_amount_nonneg", "vendor_payments", type_="check"
    )
    op.create_check_constraint(
        "ck_vendor_payments_amount_pos", "vendor_payments", "amount > 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_vendor_payments_amount_pos", "vendor_payments", type_="check"
    )
    op.create_check_constraint(
        "ck_vendor_payments_amount_nonneg", "vendor_payments", "amount >= 0"
    )
