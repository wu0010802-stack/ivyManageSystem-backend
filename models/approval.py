"""models/approval.py — 多級簽核政策、稽核記錄與審核狀態 dual-write。

ApprovalPolicy / ApprovalLog：多級簽核政策與稽核記錄。
ApprovalStatus / register_p1_listeners：審核狀態 enum 與 P1 雙寫 listener。

P1 期間：callsite 仍寫 `record.is_approved = True/False/None`，
listener 自動同步 `record.status = 'approved'/'rejected'/'pending'`。
P2 PR 會反轉方向（status → is_approved），P4 PR 會移除 listener。
詳見 docs/superpowers/specs/2026-05-26-approval-status-enum-rollout-design.md §3.4。
"""

import enum
from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Index, event

from models.base import Base


class ApprovalStatus(str, enum.Enum):
    """共用審核狀態，由 LeaveRecord / OvertimeRecord / PunchCorrectionRequest 三表使用。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# 不要 import ApprovalStatus 到 alembic migration — 用 frozen mapping。
_BOOL_TO_STATUS = {
    True: ApprovalStatus.APPROVED.value,
    False: ApprovalStatus.REJECTED.value,
    None: ApprovalStatus.PENDING.value,
}


def register_p1_listeners(*model_classes) -> None:
    """為傳入的 model class 註冊 is_approved → status 單向同步 listener。

    P1+P2 期間使用。每個 class 必須有 `is_approved` 與 `status` 兩個 Column。
    `propagate=False` 防止繼承時重複掛載。
    """

    for cls in model_classes:
        _register_one(cls)


def _register_one(cls) -> None:
    @event.listens_for(cls.is_approved, "set", propagate=False)
    def _sync_status(target, value, oldvalue, initiator):
        expected = _BOOL_TO_STATUS[value]
        # Idempotency guard：已對齊就不再寫，避免無謂 UPDATE。
        if target.status != expected:
            target.status = expected


class ApprovalPolicy(Base):
    """審核資格政策表：定義哪些角色可審核哪些角色的申請"""
    __tablename__ = "approval_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doc_type = Column(String(20), nullable=False, default="all",
                      comment="文件類型：all 表示套用於所有類型")
    submitter_role = Column(String(20), nullable=False,
                            comment="申請人角色：teacher / supervisor / hr / admin")
    approver_roles = Column(String(100), nullable=False,
                            comment="可審核的角色，逗號分隔：supervisor,hr,admin")
    is_active = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ApprovalLog(Base):
    """簽核記錄表：每次審核動作的完整稽核歷程"""
    __tablename__ = "approval_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    doc_type = Column(String(20), nullable=False,
                      comment="文件類型：leave / overtime / punch_correction")
    doc_id = Column(Integer, nullable=False, comment="對應文件 ID")
    action = Column(String(20), nullable=False,
                    comment="操作：approved / rejected / cancelled")
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approver_username = Column(String(50), nullable=False, comment="審核者帳號")
    approver_role = Column(String(20), nullable=True, comment="審核者角色")
    comment = Column(Text, nullable=True, comment="駁回原因或備註")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_approval_log_doc", "doc_type", "doc_id"),
    )
