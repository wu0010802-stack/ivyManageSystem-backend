"""portfolio batch a: attachments, observations, allergies, medication orders/logs

Revision ID: y4u5v6w7x8y9
Revises: x3t4u5v6w7x8
Create Date: 2026-04-24

Why:
  新增「幼兒成長歷程 / Portfolio」模組 Batch A：
  - attachments                  多型附件（掛 observation / report / medication_order）
  - student_observations         日常正向觀察（與 student_incidents 並存）
  - student_allergies            長期過敏資訊（結構化取代 Student.allergy 純文字欄位）
  - student_medication_orders    當日臨時用藥單
  - student_medication_logs      餵藥執行紀錄（append-only；DB trigger 拒絕修改已執行的 log，
                                 修正請透過 correction_of 新增一筆修正 log）

參考規格：/Users/yilunwu/.claude/plans/portfolio-flickering-duckling.md
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "y4u5v6w7x8y9"
down_revision = "x3t4u5v6w7x8"
branch_labels = None
depends_on = None


# ── Tables ──────────────────────────────────────────────────────────────
T_ATTACH = "attachments"
T_OBS = "student_observations"
T_ALLERGY = "student_allergies"
T_MED_ORDER = "student_medication_orders"
T_MED_LOG = "student_medication_logs"


def _table_exists(bind, name: str) -> bool:
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # attachments
    if not _table_exists(bind, T_ATTACH):
        op.create_table(
            T_ATTACH,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_type", sa.String(length=30), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("storage_key", sa.String(length=255), nullable=False),
            sa.Column("display_key", sa.String(length=255), nullable=True),
            sa.Column("thumb_key", sa.String(length=255), nullable=True),
            sa.Column("original_filename", sa.String(length=255), nullable=False),
            sa.Column("mime_type", sa.String(length=100), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column(
                "uploaded_by",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_attachments_owner", T_ATTACH, ["owner_type", "owner_id"])
        op.create_index("ix_attachments_uploaded_by", T_ATTACH, ["uploaded_by"])
        op.create_index("ix_attachments_deleted_at", T_ATTACH, ["deleted_at"])

    # student_observations
    if not _table_exists(bind, T_OBS):
        op.create_table(
            T_OBS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "student_id",
                sa.Integer(),
                sa.ForeignKey("students.id"),
                nullable=False,
            ),
            sa.Column("observation_date", sa.Date(), nullable=False),
            sa.Column("domain", sa.String(length=30), nullable=True),
            sa.Column("narrative", sa.Text(), nullable=False),
            sa.Column("rating", sa.SmallInteger(), nullable=True),
            sa.Column(
                "is_highlight",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "recorded_by",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_student_observations_student_date",
            T_OBS,
            ["student_id", "observation_date"],
        )
        op.create_index("ix_student_observations_highlight", T_OBS, ["is_highlight"])
        op.create_index("ix_student_observations_deleted_at", T_OBS, ["deleted_at"])

    # student_allergies
    if not _table_exists(bind, T_ALLERGY):
        op.create_table(
            T_ALLERGY,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "student_id",
                sa.Integer(),
                sa.ForeignKey("students.id"),
                nullable=False,
            ),
            sa.Column("allergen", sa.String(length=100), nullable=False),
            sa.Column("severity", sa.String(length=10), nullable=False),
            sa.Column("reaction_symptom", sa.String(length=200), nullable=True),
            sa.Column("first_aid_note", sa.Text(), nullable=True),
            sa.Column(
                "active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_student_allergies_student_active", T_ALLERGY, ["student_id", "active"]
        )

    # student_medication_orders
    if not _table_exists(bind, T_MED_ORDER):
        # time_slots 在 PG 用 JSONB，其他 dialect 用 JSON（SQLAlchemy 自動映射）
        from sqlalchemy.dialects.postgresql import JSONB

        time_slots_type = JSONB if is_postgres else sa.JSON
        op.create_table(
            T_MED_ORDER,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "student_id",
                sa.Integer(),
                sa.ForeignKey("students.id"),
                nullable=False,
            ),
            sa.Column("order_date", sa.Date(), nullable=False),
            sa.Column("medication_name", sa.String(length=100), nullable=False),
            sa.Column("dose", sa.String(length=50), nullable=False),
            sa.Column("time_slots", time_slots_type, nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "source",
                sa.String(length=20),
                nullable=False,
                server_default="teacher",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_medication_orders_student_date",
            T_MED_ORDER,
            ["student_id", "order_date"],
        )
        op.create_index("ix_medication_orders_date", T_MED_ORDER, ["order_date"])

    # student_medication_logs
    if not _table_exists(bind, T_MED_LOG):
        op.create_table(
            T_MED_LOG,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "order_id",
                sa.Integer(),
                sa.ForeignKey("student_medication_orders.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scheduled_time", sa.String(length=5), nullable=False),
            sa.Column("administered_at", sa.DateTime(), nullable=True),
            sa.Column(
                "administered_by",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "skipped",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("skipped_reason", sa.String(length=200), nullable=True),
            sa.Column("note", sa.String(length=200), nullable=True),
            sa.Column(
                "correction_of",
                sa.Integer(),
                sa.ForeignKey("student_medication_logs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        # Partial unique：僅 correction_of IS NULL 的「原始 log」需要唯一
        if is_postgres:
            op.execute(
                sa.text(
                    "CREATE UNIQUE INDEX uq_medication_logs_order_slot_primary "
                    "ON student_medication_logs (order_id, scheduled_time) "
                    "WHERE correction_of IS NULL"
                )
            )
        else:
            # SQLite 也支援 partial index
            op.execute(
                sa.text(
                    "CREATE UNIQUE INDEX uq_medication_logs_order_slot_primary "
                    "ON student_medication_logs (order_id, scheduled_time) "
                    "WHERE correction_of IS NULL"
                )
            )
        op.create_index(
            "ix_medication_logs_administered_at", T_MED_LOG, ["administered_at"]
        )
        op.create_index(
            "ix_medication_logs_correction_of", T_MED_LOG, ["correction_of"]
        )

    # ── DB trigger：拒絕對已 administered 或 skipped 的 log 做 UPDATE ────
    # 確保真正不可變，不依賴應用層（advisor 指出的 blocker #2）
    if is_postgres:
        op.execute(sa.text("""
                CREATE OR REPLACE FUNCTION medication_log_immutable_fn()
                RETURNS trigger AS $$
                BEGIN
                    IF (OLD.administered_at IS NOT NULL OR OLD.skipped IS TRUE) THEN
                        RAISE EXCEPTION '已執行 / 已跳過的餵藥紀錄不可修改，請改用 /correct 端點新增修正紀錄（log_id=%）', OLD.id;
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """))
        op.execute(sa.text("""
                CREATE TRIGGER trg_medication_log_immutable
                BEFORE UPDATE ON student_medication_logs
                FOR EACH ROW
                EXECUTE FUNCTION medication_log_immutable_fn();
                """))
    else:
        # SQLite：用 BEFORE UPDATE trigger + RAISE(ABORT)
        op.execute(sa.text("""
                CREATE TRIGGER trg_medication_log_immutable
                BEFORE UPDATE ON student_medication_logs
                FOR EACH ROW
                WHEN OLD.administered_at IS NOT NULL OR OLD.skipped = 1
                BEGIN
                    SELECT RAISE(ABORT, '已執行 / 已跳過的餵藥紀錄不可修改');
                END;
                """))


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Drop trigger
    if is_postgres:
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_medication_log_immutable ON student_medication_logs"
            )
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS medication_log_immutable_fn()"))
    else:
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_medication_log_immutable"))

    for tbl in (T_MED_LOG, T_MED_ORDER, T_ALLERGY, T_OBS, T_ATTACH):
        if _table_exists(bind, tbl):
            op.drop_table(tbl)
