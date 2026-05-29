"""P0c-1 consent schema: parent_consent_log + policy_versions

Revision ID: consent01
Revises: mergeheads06
Create Date: 2026-05-28

P0 法規/個資 sprint 第三件 Phase 1：建立 consent 追蹤基礎設施。

兩張表：
- policy_versions: 隱私權政策版本（v1 / v2 / ...），含 effective_at + document_path
- parent_consent_log: 每筆 = 一次同意/撤回事件（scope-aware）

Scope 列舉值：
- service_essential   服務必要（必同意才能用 portal）
- photo_publish       照片公開（可單獨撤回）
- line_push           LINE 推播（可單獨撤回）
- cross_border_transfer  跨境傳輸（Supabase US region）

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "consent01"
down_revision = "mergeheads06"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "version",
            sa.String(20),
            nullable=False,
            unique=True,
            comment="版本字串如 '2026.1'",
        ),
        sa.Column("effective_at", sa.DateTime, nullable=False, comment="政策生效時間"),
        sa.Column(
            "document_path",
            sa.String(255),
            nullable=False,
            comment="storage key 指向 PDF/HTML 版本文件",
        ),
        sa.Column(
            "summary",
            sa.Text,
            nullable=True,
            comment="中文版本說明（給 LIFF modal 顯示變動摘要）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_policy_versions_effective_at", "policy_versions", ["effective_at"]
    )

    op.create_table(
        "parent_consent_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "policy_version_id",
            sa.Integer,
            sa.ForeignKey("policy_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "scope",
            sa.String(50),
            nullable=False,
            comment="service_essential / photo_publish / line_push / cross_border_transfer",
        ),
        sa.Column(
            "consented",
            sa.Boolean,
            nullable=False,
            comment="true=同意, false=撤回（同樣寫入 log）",
        ),
        sa.Column(
            "consented_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "ip_address", sa.String(45), nullable=True, comment="IPv6 max length"
        ),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "note",
            sa.Text,
            nullable=True,
            comment="撤回理由、特殊情境記載",
        ),
    )
    op.create_index(
        "ix_pcl_user_scope_time",
        "parent_consent_log",
        ["user_id", "scope", "consented_at"],
    )


def downgrade():
    op.drop_index("ix_pcl_user_scope_time", table_name="parent_consent_log")
    op.drop_table("parent_consent_log")
    op.drop_index("ix_policy_versions_effective_at", table_name="policy_versions")
    op.drop_table("policy_versions")
