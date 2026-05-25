"""notification consolidation (rename + notification_logs)

Revision ID: notif01_consolidation
Revises: mergeheads02
Create Date: 2026-05-25

操作：
1. rename parent_notification_preferences → notification_preferences
2. ALTER CONSTRAINT uq_parent_notif_pref_triple → uq_notif_pref_triple (PG only)
3. create index ix_notif_pref_user_event
4. UPDATE event_type 加 'parent.' 前綴（既有 7 個值）
5. CREATE TABLE notification_logs + 三 index

downgrade 反向。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "notif01_consolidation"
down_revision = "mergeheads02"
branch_labels = None
depends_on = None


PARENT_OLD_EVENT_TYPES = (
    "message_received",
    "announcement",
    "event_ack_required",
    "fee_due",
    "leave_result",
    "attendance_alert",
    "contact_book_published",
)


def upgrade() -> None:
    # 1. rename 表
    op.rename_table("parent_notification_preferences", "notification_preferences")

    # 2. constraint rename — PG only；SQLite 不支援 ALTER CONSTRAINT
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE notification_preferences "
            "RENAME CONSTRAINT uq_parent_notif_pref_triple TO uq_notif_pref_triple"
        )

    # 3. index
    op.create_index(
        "ix_notif_pref_user_event",
        "notification_preferences",
        ["user_id", "event_type"],
    )

    # 4. backfill event_type 前綴
    in_clause = ",".join(f"'{ev}'" for ev in PARENT_OLD_EVENT_TYPES)
    op.execute(
        f"UPDATE notification_preferences "
        f"SET event_type = 'parent.' || event_type "
        f"WHERE event_type IN ({in_clause})"
    )

    # 5. create notification_logs
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "recipient_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column(
            "sender_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("payload_json", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("source_entity_type", sa.String(40), nullable=True),
        sa.Column("source_entity_id", sa.Integer, nullable=True),
        sa.Column("deep_link", sa.String(255), nullable=True),
        sa.Column("channels_attempted", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("channels_succeeded", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("channels_failed", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("read_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # PG partial index for unread；SQLite 不支援 postgresql_where 但 alembic fallback skip
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_notif_log_recipient_unread",
            "notification_logs",
            ["recipient_user_id", "read_at"],
            postgresql_where=sa.text("read_at IS NULL"),
        )
    else:
        op.create_index(
            "ix_notif_log_recipient_unread",
            "notification_logs",
            ["recipient_user_id", "read_at"],
        )
    op.create_index(
        "ix_notif_log_recipient_created",
        "notification_logs",
        ["recipient_user_id", "created_at"],
    )
    op.create_index(
        "ix_notif_log_source",
        "notification_logs",
        ["source_entity_type", "source_entity_id"],
    )


def downgrade() -> None:
    """反向 upgrade。

    為支援 stamp-only DB（Alembic Roundtrip CI 從 metadata.create_all 建表後直接
    stamp 到 head 再跑 downgrade）下的健全性，所有 DROP / rename 都使用 IF EXISTS
    或先 SELECT 偵測 — 不存在的物件不應阻擋 downgrade。
    """
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 反向 5: drop notification_logs (含 3 indexes)
    if is_pg:
        op.execute("DROP INDEX IF EXISTS ix_notif_log_source")
        op.execute("DROP INDEX IF EXISTS ix_notif_log_recipient_created")
        op.execute("DROP INDEX IF EXISTS ix_notif_log_recipient_unread")
        op.execute("DROP TABLE IF EXISTS notification_logs")
    else:
        for idx in (
            "ix_notif_log_source",
            "ix_notif_log_recipient_created",
            "ix_notif_log_recipient_unread",
        ):
            try:
                op.drop_index(idx, table_name="notification_logs")
            except Exception:
                pass
        try:
            op.drop_table("notification_logs")
        except Exception:
            pass

    # 反向 4: 取消 event_type 前綴 (stamp-only DB 無 row → 0 rows affected, 安全)
    in_clause = ",".join(f"'parent.{ev}'" for ev in PARENT_OLD_EVENT_TYPES)
    op.execute(
        f"UPDATE notification_preferences "
        f"SET event_type = SUBSTR(event_type, 8) "
        f"WHERE event_type IN ({in_clause})"
    )

    # 反向 3: drop index (stamp-only DB 無此 index → IF EXISTS 安全)
    if is_pg:
        op.execute("DROP INDEX IF EXISTS ix_notif_pref_user_event")
    else:
        try:
            op.drop_index(
                "ix_notif_pref_user_event", table_name="notification_preferences"
            )
        except Exception:
            pass

    # 反向 2: rename constraint (stamp-only DB constraint 仍是舊名)
    if is_pg:
        op.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_notif_pref_triple'
                ) THEN
                    ALTER TABLE notification_preferences
                    RENAME CONSTRAINT uq_notif_pref_triple TO uq_parent_notif_pref_triple;
                END IF;
            END $$;
            """)

    # 反向 1: rename table back (stamp-only DB 表已是新名 → 偵測後 rename；舊名 → 跳過)
    if is_pg:
        op.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'notification_preferences'
                ) THEN
                    ALTER TABLE notification_preferences
                    RENAME TO parent_notification_preferences;
                END IF;
            END $$;
            """)
    else:
        try:
            op.rename_table(
                "notification_preferences", "parent_notification_preferences"
            )
        except Exception:
            pass
