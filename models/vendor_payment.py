"""
models/vendor_payment.py — 廠商付款簽收

園所對廠商付款（清潔用品、教具、食材等）的紙本流數位化：
登錄付款項目 → 收集廠商簽收（簽名或照片）→ 留稽核痕跡。
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
PAYMENT_STATUSES = ("pending", "signed")
SIGNATURE_KINDS = ("drawn", "photo")


class VendorPayment(Base):
    __tablename__ = "vendor_payments"

    id = Column(Integer, primary_key=True)
    payment_date = Column(Date, nullable=False, index=True)
    vendor_name = Column(String(120), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(20), nullable=False)
    description = Column(String(255))
    invoice_number = Column(String(60))
    notes = Column(Text)
    # 附件 list；每筆: {key, filename, size, mime_type, uploaded_at}
    # JSON.with_variant(JSONB, postgresql) — sqlite test 自動 fallback 到 JSON
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
        CheckConstraint("amount > 0", name="ck_vendor_payments_amount_pos"),
        CheckConstraint(
            "payment_method IN ('cash','bank_transfer','check','linepay','other')",
            name="ck_vendor_payments_method",
        ),
        CheckConstraint(
            "status IN ('pending','signed')", name="ck_vendor_payments_status"
        ),
        CheckConstraint(
            "signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
            name="ck_vendor_payments_signature_kind",
        ),
        Index("ix_vendor_payments_status_date", "status", "payment_date"),
    )
