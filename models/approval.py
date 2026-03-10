"""
models/approval.py — 多級簽核政策與稽核記錄
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Index

from models.base import Base


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
