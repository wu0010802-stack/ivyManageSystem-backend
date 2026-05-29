"""P0c-2 DSR requests table

Revision ID: dsrreq01
Revises: mergeheads06
Create Date: 2026-05-28

P0 法規/個資 sprint 第三件 Phase 2：個資法 §3 五權（delete/correct/objection）+ §11。

Type 列舉：
- delete   刪除請求（家長申請刪除子女資料 → admin review → 觸發 student_lifecycle）
- correct  更正請求（家長申請更正欄位 + 新值 + 理由 → admin review → apply diff）
- opt_out  停止處理請求（家長要求停止特定 scope，比 consent 撤回更具法律意義）

Status 列舉：
- pending      待 admin review（建立時預設）
- approved     admin 同意執行（可記 admin_user_id + decision_note）
- rejected     admin 拒絕（必記 decision_note 說明理由）

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.2 DSR endpoints
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "dsrreq01"
down_revision = "consent01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dsr_requests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="申請人 user_id（家長或員工）",
        ),
        sa.Column(
            "request_type",
            sa.String(20),
            nullable=False,
            comment="delete / correct / opt_out",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
            comment="pending / approved / rejected",
        ),
        sa.Column(
            "subject_entity_type",
            sa.String(50),
            nullable=True,
            comment="目標 entity 類型（student / employee / guardian）",
        ),
        sa.Column(
            "subject_entity_id",
            sa.Integer,
            nullable=True,
            comment="目標 entity ID（如 student_id），與 subject_entity_type 對應",
        ),
        sa.Column(
            "field_name",
            sa.String(50),
            nullable=True,
            comment="correct 用：要更正的欄位名",
        ),
        sa.Column(
            "new_value",
            sa.Text,
            nullable=True,
            comment="correct 用：要更正的新值（字串化）",
        ),
        sa.Column(
            "scope",
            sa.String(50),
            nullable=True,
            comment="opt_out 用：要停止處理的 scope（對齊 consent scope）",
        ),
        sa.Column("reason", sa.Text, nullable=True, comment="申請理由"),
        sa.Column(
            "submitted_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.DateTime, nullable=True, comment="admin 處理時間"),
        sa.Column(
            "decided_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="處理的 admin user_id",
        ),
        sa.Column("decision_note", sa.Text, nullable=True, comment="admin 決議說明"),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_dsr_status_submitted", "dsr_requests", ["status", "submitted_at"]
    )
    op.create_index("ix_dsr_user_type", "dsr_requests", ["user_id", "request_type"])


def downgrade():
    op.drop_index("ix_dsr_user_type", table_name="dsr_requests")
    op.drop_index("ix_dsr_status_submitted", table_name="dsr_requests")
    op.drop_table("dsr_requests")
