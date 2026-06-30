"""
models/misc_receipt.py — 雜項收款簽收

園所對學費/活動以外雜項進帳（場地租金、捐款、補助款、二手義賣、退費回收等）
的紙本流數位化：登錄收款項目 → 收集繳款方簽收（簽名或照片）→ 留稽核痕跡。
與「廠商付款簽收」(vendor_payments) 鏡像對稱，方向相反（收入側）。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    CheckConstraint,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base

PAYMENT_METHODS = ("cash", "bank_transfer", "check", "linepay", "other")
RECEIPT_STATUSES = ("pending", "signed")
SIGNATURE_KINDS = ("drawn", "photo")
RECEIPT_CATEGORIES = (
    "rent",
    "donation",
    "subsidy",
    "secondhand_sale",
    "refund_recovery",
    "other",
)


class MiscReceipt(Base):
    __tablename__ = "misc_receipts"

    id = Column(Integer, primary_key=True)
    receipt_date = Column(Date, nullable=False, index=True)
    payer_name = Column(String(120), nullable=False, index=True)
    category = Column(String(20), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(20), nullable=False)
    description = Column(String(255))
    receipt_number = Column(String(60))
    notes = Column(Text)
    attachments = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )

    status = Column(String(16), nullable=False, default="pending")
    signer_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    signed_at = Column(DateTime)
    signature_kind = Column(String(16))  # drawn | photo | NULL
    signature_key = Column(String(255))

    created_by_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"))
    created_at = Column(DateTime, nullable=False, default=now_taipei_naive)
    updated_at = Column(
        DateTime, nullable=False, default=now_taipei_naive, onupdate=now_taipei_naive
    )

    signer = relationship("Employee", foreign_keys=[signer_id], lazy="joined")
    created_by = relationship("Employee", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_misc_receipts_amount_pos"),
        CheckConstraint(
            "payment_method IN ('cash','bank_transfer','check','linepay','other')",
            name="ck_misc_receipts_method",
        ),
        CheckConstraint(
            "status IN ('pending','signed')", name="ck_misc_receipts_status"
        ),
        CheckConstraint(
            "category IN ('rent','donation','subsidy','secondhand_sale','refund_recovery','other')",
            name="ck_misc_receipts_category",
        ),
        CheckConstraint(
            "signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
            name="ck_misc_receipts_signature_kind",
        ),
        Index("ix_misc_receipts_status_date", "status", "receipt_date"),
    )
