"""fix student_fee_records.student_id FK from CASCADE to RESTRICT

NV8 修復：防止刪除學生時靜默刪除繳費歷史記錄（財務稽核要求）。

Revision ID: v1w2x3y4z5a6
Revises: u2v3w4x5y6z7
Create Date: 2026-03-27 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect


revision = "v1w2x3y4z5a6"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "student_fee_records" not in tables:
        return  # 資料表尚未建立，跳過

    # 取得現有 FK constraint 名稱（PostgreSQL 自動命名為 <table>_<col>_fkey）
    fk_name = None
    for fk in inspector.get_foreign_keys("student_fee_records"):
        if fk.get("constrained_columns") == ["student_id"]:
            fk_name = fk.get("name")
            break

    if fk_name:
        op.drop_constraint(fk_name, "student_fee_records", type_="foreignkey")

    op.create_foreign_key(
        "student_fee_records_student_id_fkey",
        "student_fee_records",
        "students",
        ["student_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "student_fee_records" not in tables:
        return

    op.drop_constraint(
        "student_fee_records_student_id_fkey",
        "student_fee_records",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "student_fee_records_student_id_fkey",
        "student_fee_records",
        "students",
        ["student_id"],
        ["id"],
        ondelete="CASCADE",
    )
