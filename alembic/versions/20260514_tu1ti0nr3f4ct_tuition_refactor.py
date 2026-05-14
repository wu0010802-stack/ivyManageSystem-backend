"""tuition refactor c2: drop fee_items, broaden fee_templates CHECK

c2 退場 FeeItem 表並讓 student_fee_records.fee_item_id 變 nullable（c3 將砍 column）。
擴張 fee_templates.ck_fee_template_type 由 3 種放寬至 5 種（含 material / insurance），
為後續年級彈性費用範本鋪路。

操作順序（upgrade）：
1. ALTER student_fee_records.fee_item_id DROP NOT NULL
2. DROP CONSTRAINT uq_student_fee_item（fee_item_id NULL 後唯一鍵失去意義；
   monthly 冪等改靠 ix_fee_records_monthly_unique）
3. DROP FK student_fee_records.fee_item_id → fee_items.id
4. DROP TABLE fee_items
5. fee_templates.ck_fee_template_type 改為 5 種

downgrade 對稱還原，schema-only 不 backfill。

Revision ID: tu1ti0nr3f4ct
Revises: m1m2m3m4m5m6
Create Date: 2026-05-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "tu1ti0nr3f4ct"
down_revision = "m1m2m3m4m5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 1) fee_item_id 改 nullable ───────────────────────────────────────
    if "student_fee_records" in tables:
        op.alter_column(
            "student_fee_records",
            "fee_item_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

        # ── 2) drop uq_student_fee_item ─────────────────────────────────
        existing_uniques = {
            uq.get("name")
            for uq in inspector.get_unique_constraints("student_fee_records")
        }
        if "uq_student_fee_item" in existing_uniques:
            op.drop_constraint(
                "uq_student_fee_item", "student_fee_records", type_="unique"
            )

        # ── 3) drop FK to fee_items ─────────────────────────────────────
        fk_name = None
        for fk in inspector.get_foreign_keys("student_fee_records"):
            if fk.get("constrained_columns") == ["fee_item_id"]:
                fk_name = fk.get("name")
                break
        if fk_name:
            op.drop_constraint(fk_name, "student_fee_records", type_="foreignkey")

    # ── 4) drop fee_items table ─────────────────────────────────────────
    if "fee_items" in tables:
        # 索引在 DROP TABLE 時自動清掉，不需手動
        op.drop_table("fee_items")

    # ── 5) fee_templates CHECK 由 3 → 5 種 ──────────────────────────────
    if "fee_templates" in tables:
        op.drop_constraint("ck_fee_template_type", "fee_templates", type_="check")
        op.create_check_constraint(
            "ck_fee_template_type",
            "fee_templates",
            "fee_type IN ('registration','miscellaneous','monthly','material','insurance')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 5') CHECK 還原為 3 種 ───────────────────────────────────────────
    if "fee_templates" in tables:
        op.drop_constraint("ck_fee_template_type", "fee_templates", type_="check")
        op.create_check_constraint(
            "ck_fee_template_type",
            "fee_templates",
            "fee_type IN ('registration','miscellaneous','monthly')",
        )

    # ── 4') 重建 fee_items 表（schema 對照 c1 前 models/fees.py:96-121）─
    if "fee_items" not in tables:
        op.create_table(
            "fee_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column(
                "classroom_id",
                sa.Integer(),
                sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("period", sa.String(length=20), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=True, server_default=sa.text("true")
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_fee_items_period_active",
            "fee_items",
            ["period", "is_active"],
        )
        op.create_index(
            "ix_fee_items_classroom",
            "fee_items",
            ["classroom_id"],
        )

    # ── 3') 還原 FK ─────────────────────────────────────────────────────
    if "student_fee_records" in tables:
        op.create_foreign_key(
            "student_fee_records_fee_item_id_fkey",
            "student_fee_records",
            "fee_items",
            ["fee_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )

        # ── 2') 還原 uq_student_fee_item ───────────────────────────────
        op.create_unique_constraint(
            "uq_student_fee_item",
            "student_fee_records",
            ["student_id", "fee_item_id"],
        )

        # ── 1') fee_item_id 還原為 NOT NULL ───────────────────────────
        # 若有 NULL row 將失敗：downgrade 需求方自行處理
        op.alter_column(
            "student_fee_records",
            "fee_item_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
