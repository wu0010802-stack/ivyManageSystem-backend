"""appraisal_summary_log: 簽核軌跡表（Phase 2 signing UX）

Revision ID: aprsig001
Revises: aprcal001
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "aprsig001"
down_revision = "aprcal001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 建 PostgreSQL enum type for action
    op.execute("""
        CREATE TYPE appraisal_summary_action AS ENUM (
            'SIGN_SUPERVISOR', 'SIGN_ACCOUNTING', 'FINALIZE',
            'REJECT', 'COMMENT', 'RECOMPUTE'
        )
        """)

    # 2. 建 appraisal_summary_log table
    op.create_table(
        "appraisal_summary_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "summary_id",
            sa.BigInteger,
            sa.ForeignKey("appraisal_summaries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "action",
            postgresql.ENUM(name="appraisal_summary_action", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "from_status",
            postgresql.ENUM(name="appraisal_summary_status_enum", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            postgresql.ENUM(name="appraisal_summary_status_enum", create_type=False),
            nullable=True,
        ),
        # bug sweep 2026-05-18 P2 文件化：actor_id 用 RESTRICT（與其他模組 SET NULL
        # 慣例不同但刻意為之）。本表是「簽核軌跡」審計表，actor 是強身分證明；
        # 若 admin 帳號被刪而把 actor_id 設 NULL，等於抹掉「誰簽核的」這條稽核
        # 關鍵欄位，違反金流類審計表「人類處理 user 刪除事件」的原則。RESTRICT
        # 會在嘗試刪除有 log 的 user 時擋下，強制 admin 先選擇：
        #   (a) 該 user 改 is_active=False 而非物理刪除（推薦）
        #   (b) 用 SQL 把 actor_role_snapshot 保留下備查再清 log（極少數情境）
        sa.Column(
            "actor_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_role_snapshot", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_summary_log_summary",
        "appraisal_summary_log",
        ["summary_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_summary_log_summary", table_name="appraisal_summary_log")
    op.drop_table("appraisal_summary_log")
    op.execute("DROP TYPE IF EXISTS appraisal_summary_action")
