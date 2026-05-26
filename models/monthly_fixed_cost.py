"""
models/monthly_fixed_cost.py — 月度固定費用（手動登錄）

Phase 2 新增：用於月度損益表的「變動支出」與「人事支出（舊制勞退）」section。
與 vendor_payments 為獨立資料源：vendor_payments 紀錄個別廠商付款流水（含
簽收），本表登錄固定每月支出（租金 / 零用金 / 水電費 / 餐點 / 舊制勞退準備金），
兩者在 monthly_pnl 不去重（user 自行區分登錄）。

唯一鍵 (year, month, category)：每月每類別只有一筆，前端以試算表編輯後 batch upsert。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    CheckConstraint,
)

from models.base import Base
from models.types import Money

# 與 DB CheckConstraint 同步維護的合法 category list
FIXED_COST_CATEGORIES = (
    "rent",
    "office_petty_cash",
    "kitchen_petty_cash",
    "meals",
    "water",
    "electricity",
    "phone",
    "old_pension_reserve",
)


class MonthlyFixedCost(Base):
    __tablename__ = "monthly_fixed_costs"

    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False, index=True)
    month = Column(Integer, nullable=False)  # 1-12
    category = Column(String(40), nullable=False)
    amount = Column(Money, nullable=False, default=0)
    notes = Column(Text)

    created_at = Column(DateTime, nullable=False, default=now_taipei_naive)
    updated_at = Column(
        DateTime, nullable=False, default=now_taipei_naive, onupdate=now_taipei_naive
    )
    created_by_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "year", "month", "category", name="uq_monthly_fixed_costs_period_cat"
        ),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_monthly_fixed_costs_month"),
        CheckConstraint("amount >= 0", name="ck_monthly_fixed_costs_amount_nonneg"),
        CheckConstraint(
            "category IN ('rent','office_petty_cash','kitchen_petty_cash','meals',"
            "'water','electricity','phone','old_pension_reserve')",
            name="ck_monthly_fixed_costs_category",
        ),
        Index("ix_monthly_fixed_costs_year_month", "year", "month"),
    )
