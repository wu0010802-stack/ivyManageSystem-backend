"""models/consent.py — 家長同意 / 政策版本追蹤。

P0c 法規/個資 sprint 第三件 Phase 1：個資法 §8 告知義務 / §19 特定目的必要範圍 /
GDPR Art. 7 demonstrable consent 的基礎設施。

兩張表：
- PolicyVersion: 隱私權政策版本（v1 / v2 / ...），policy 升版即新增一列
- ParentConsentLog: 每筆 = 家長一次同意 / 撤回事件，scope-aware

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.1
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from models.base import Base
from utils.taipei_time import now_taipei_naive

# Scope 列舉值（用 string 不用 Enum，避免 alembic enum migration 複雜性）
CONSENT_SCOPE_SERVICE_ESSENTIAL = "service_essential"
CONSENT_SCOPE_PHOTO_PUBLISH = "photo_publish"
CONSENT_SCOPE_LINE_PUSH = "line_push"
CONSENT_SCOPE_CROSS_BORDER_TRANSFER = "cross_border_transfer"

CONSENT_SCOPES: frozenset[str] = frozenset(
    {
        CONSENT_SCOPE_SERVICE_ESSENTIAL,
        CONSENT_SCOPE_PHOTO_PUBLISH,
        CONSENT_SCOPE_LINE_PUSH,
        CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
    }
)


class PolicyVersion(Base):
    """隱私權政策版本。每次政策升版新增一列，既有家長下次登入強制重簽。"""

    __tablename__ = "policy_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(
        String(20), nullable=False, unique=True, comment="版本字串如 '2026.1'"
    )
    effective_at = Column(DateTime, nullable=False, comment="政策生效時間")
    document_path = Column(
        String(255),
        nullable=False,
        comment="storage key 指向 PDF/HTML 版本文件",
    )
    summary = Column(
        Text,
        nullable=True,
        comment="中文版本說明（給 LIFF modal 顯示變動摘要）",
    )
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (Index("ix_policy_versions_effective_at", "effective_at"),)


class ParentConsentLog(Base):
    """家長同意事件 log。每筆 = 一次同意 / 撤回（撤回也寫入，consented=False）。

    查詢「家長 X 對 scope Y 的最新狀態」：
      SELECT consented FROM parent_consent_log
      WHERE user_id = :uid AND scope = :scope
      ORDER BY consented_at DESC LIMIT 1
    """

    __tablename__ = "parent_consent_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        # SET NULL：硬刪 user 時保留同意證明稽核（RA-MED-9），非 CASCADE 連坐刪除
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    policy_version_id = Column(
        Integer, ForeignKey("policy_versions.id"), nullable=False
    )
    scope = Column(
        String(50),
        nullable=False,
        comment="service_essential / photo_publish / line_push / cross_border_transfer",
    )
    consented = Column(
        Boolean,
        nullable=False,
        comment="true=同意, false=撤回（同樣寫入 log）",
    )
    consented_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    ip_address = Column(String(45), nullable=True, comment="IPv6 max length")
    user_agent = Column(Text, nullable=True)
    note = Column(Text, nullable=True, comment="撤回理由、特殊情境記載")

    __table_args__ = (
        Index("ix_pcl_user_scope_time", "user_id", "scope", "consented_at"),
    )
