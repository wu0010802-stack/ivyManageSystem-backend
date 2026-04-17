"""
models/fees.py — 學費/費用管理資料模型
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Date,
    Text,
    ForeignKey,
    UniqueConstraint,
    Index,
)

from models.base import Base


class FeeItem(Base):
    """費用項目：定義一種費用的名稱、金額、適用班級與學期"""

    __tablename__ = "fee_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(
        String(100), nullable=False, comment="費用名稱（學費/雜費/材料費...）"
    )
    amount = Column(Integer, nullable=False, comment="金額（元）")
    classroom_id = Column(
        Integer,
        ForeignKey("classrooms.id", ondelete="SET NULL"),
        nullable=True,
        comment="適用班級（NULL=全校適用）",
    )
    period = Column(String(20), nullable=False, comment="學年學期（e.g. 2025-1）")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_fee_items_period_active", "period", "is_active"),
        Index("ix_fee_items_classroom", "classroom_id"),
    )


class StudentFeeRecord(Base):
    """學生費用記錄：學生每個費用項目的應繳與繳費狀態"""

    __tablename__ = "student_fee_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        # NV8：改為 RESTRICT 防止刪除學生時靜默級聯刪除繳費歷史（違反財務稽核要求）。
        # student_name / classroom_name 快照欄位已確保歷史記錄可讀性。
        ForeignKey("students.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # snapshot 冗餘，避免刪除學生/班級後歷史資料遺失
    student_name = Column(String(50), nullable=False, comment="學生姓名（snapshot）")
    classroom_name = Column(String(50), nullable=True, comment="班級名稱（snapshot）")

    fee_item_id = Column(
        Integer,
        ForeignKey("fee_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fee_item_name = Column(
        String(100), nullable=False, comment="費用項目名稱（snapshot）"
    )
    amount_due = Column(Integer, nullable=False, comment="應繳金額（snapshot）")
    amount_paid = Column(Integer, default=0, comment="已繳金額")

    # unpaid / paid
    status = Column(String(10), nullable=False, default="unpaid", comment="繳費狀態")
    payment_date = Column(Date, nullable=True, comment="繳費日期")
    payment_method = Column(
        String(20), nullable=True, comment="繳費方式：現金/轉帳/其他"
    )
    notes = Column(Text, nullable=True, default="")

    period = Column(
        String(20), nullable=False, comment="學年學期（denormalized，便於篩選）"
    )

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("student_id", "fee_item_id", name="uq_student_fee_item"),
        Index("ix_fee_records_period_status", "period", "status"),
        Index("ix_fee_records_student", "student_id"),
        Index("ix_fee_records_fee_item", "fee_item_id"),
        Index("ix_fee_records_student_period", "student_id", "period"),
    )


class StudentFeeRefund(Base):
    """學費退款紀錄：附加於 StudentFeeRecord 的歷史明細，不直接改動原記錄的 amount_paid。

    每次退款建立一筆紀錄，原記錄的 amount_paid 以累計繳費 - 累計退款 計算。
    刪除學費記錄時需串連處理（RESTRICT 保護）。
    """

    __tablename__ = "student_fee_refunds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(
        Integer,
        ForeignKey("student_fee_records.id", ondelete="RESTRICT"),
        nullable=False,
        comment="對應的學生費用記錄",
    )
    amount = Column(Integer, nullable=False, comment="退款金額（正整數）")
    reason = Column(String(100), nullable=False, comment="退款原因")
    notes = Column(Text, nullable=True, default="", comment="備註")
    refunded_by = Column(String(50), nullable=False, comment="操作人員 username")
    refunded_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        Index("ix_fee_refunds_record", "record_id"),
        Index("ix_fee_refunds_refunded_at", "refunded_at"),
    )
