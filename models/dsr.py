"""models/dsr.py — DSR (Data Subject Rights) 請求表。

P0c-2 法規/個資 sprint：個資法 §3 五權（查詢/更正/刪除/停止處理）+ §11。

每筆 = 一次家長或員工的 DSR 申請。實際刪除/更正/停止由 admin review 決議後
觸發既有 lifecycle / business 流程（不在 DSR 申請當下直接動 data）。

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.2
"""

from __future__ import annotations

from sqlalchemy import (
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

# Request type
DSR_REQUEST_TYPE_DELETE = "delete"
DSR_REQUEST_TYPE_CORRECT = "correct"
DSR_REQUEST_TYPE_OPT_OUT = "opt_out"

DSR_REQUEST_TYPES: frozenset[str] = frozenset(
    {DSR_REQUEST_TYPE_DELETE, DSR_REQUEST_TYPE_CORRECT, DSR_REQUEST_TYPE_OPT_OUT}
)

# Status
DSR_STATUS_PENDING = "pending"
DSR_STATUS_APPROVED = "approved"
DSR_STATUS_REJECTED = "rejected"

DSR_STATUSES: frozenset[str] = frozenset(
    {DSR_STATUS_PENDING, DSR_STATUS_APPROVED, DSR_STATUS_REJECTED}
)


class DsrRequest(Base):
    """家長/員工 DSR 申請紀錄。"""

    __tablename__ = "dsr_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        # SET NULL：硬刪 user 時保留 DSR 申請史稽核（RA-MED-9）
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="申請人 user_id（家長或員工；硬刪後 NULL 保留稽核）",
    )
    request_type = Column(
        String(20), nullable=False, comment="delete / correct / opt_out"
    )
    status = Column(String(20), nullable=False, default=DSR_STATUS_PENDING)

    subject_entity_type = Column(
        String(50),
        nullable=True,
        comment="目標 entity 類型（student / employee / guardian）",
    )
    subject_entity_id = Column(Integer, nullable=True, comment="目標 entity ID")

    # correct 用
    field_name = Column(String(50), nullable=True, comment="要更正的欄位名")
    new_value = Column(Text, nullable=True, comment="要更正的新值（字串化）")

    # opt_out 用
    scope = Column(
        String(50), nullable=True, comment="要停止處理的 scope（對齊 consent scope）"
    )

    reason = Column(Text, nullable=True, comment="申請理由")
    submitted_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    decided_at = Column(DateTime, nullable=True, comment="admin 處理時間")
    decided_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="處理的 admin user_id",
    )
    decision_note = Column(Text, nullable=True, comment="admin 決議說明")

    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_dsr_status_submitted", "status", "submitted_at"),
        Index("ix_dsr_user_type", "user_id", "request_type"),
    )
