"""未休假折算工資帳本 — per-event 紀錄。

三個 source_type：
- comp_grant_expiry：補休 +1 年到期（scheduler）
- annual_anniversary：特休週年 cutover（scheduler）
- offboarding：離職 path（Phase 2 寫入，本 spec 預留 schema）

salary_record_id 反向綁定：scheduler layer 1 直寫時 set；NULL 由 salary engine
calculate layer 2 撈取後 set。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class UnusedLeavePayoutLog(Base):
    __tablename__ = "unused_leave_payout_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hours: Mapped[float] = mapped_column(nullable=False)
    hourly_wage: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    wage_basis_date: Mapped[date] = mapped_column(Date, nullable=False)
    salary_record_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("salary_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    salary_period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    salary_period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_payout_log_emp_period",
            "employee_id",
            "salary_period_year",
            "salary_period_month",
        ),
        Index(
            "ix_payout_log_salary_record",
            "salary_record_id",
            postgresql_where=text("salary_record_id IS NOT NULL"),
        ),
        Index(
            "uq_payout_log_anniversary",
            "employee_id",
            "source_type",
            "source_ref_id",
            unique=True,
            postgresql_where=text("source_type = 'annual_anniversary'"),
            sqlite_where=text("source_type = 'annual_anniversary'"),
        ),
    )
