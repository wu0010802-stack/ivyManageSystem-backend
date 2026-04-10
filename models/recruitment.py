"""
models/recruitment.py — 招生訪視記錄
"""

from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, Index

from models.base import Base


class RecruitmentVisit(Base):
    """招生訪視記錄表"""
    __tablename__ = "recruitment_visits"

    id = Column(Integer, primary_key=True, index=True)
    month = Column(String(10), nullable=False, index=True)          # 民國月份，如 "115.03"
    seq_no = Column(String(10), nullable=True)                      # 月份內序號
    visit_date = Column(String(50), nullable=True)                  # 原始日期字串（含備注）
    child_name = Column(String(50), nullable=False)                 # 幼生姓名
    birthday = Column(Date, nullable=True)                          # 生日
    grade = Column(String(20), nullable=True)                       # 適讀班級
    phone = Column(String(100), nullable=True)                      # 電話
    address = Column(String(200), nullable=True)                    # 地址
    district = Column(String(30), nullable=True, index=True)        # 行政區
    source = Column(String(50), nullable=True, index=True)          # 幼生來源
    referrer = Column(String(50), nullable=True, index=True)        # 介紹者
    deposit_collector = Column(String(50), nullable=True)           # 收預繳人員
    has_deposit = Column(Boolean, default=False, nullable=False)    # 是否預繳
    notes = Column(Text, nullable=True)                             # 備註（含預計就讀月份）
    parent_response = Column(Text, nullable=True)                   # 電訪後家長回應

    # --- 延伸欄位（Excel 未預繳原因分析 / 近五年追蹤） ---
    no_deposit_reason = Column(String(60), nullable=True)           # 未預繳原因分類
    no_deposit_reason_detail = Column(Text, nullable=True)          # 未預繳判定說明
    enrolled = Column(Boolean, default=False, nullable=False)       # 是否已實際報到/註冊
    transfer_term = Column(Boolean, default=False, nullable=False)  # 是否轉到其他學期
    expected_start_label = Column(String(30), nullable=True)        # 預計就讀月份標籤，create/update 時自動計算

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_recruitment_month_grade",  "month",       "grade"),
        Index("ix_rv_has_deposit",           "has_deposit"),
        Index("ix_rv_no_deposit_reason",     "no_deposit_reason"),
        Index("ix_rv_has_deposit_grade",     "has_deposit", "grade"),
        Index("ix_rv_source_grade",          "source",      "grade"),
        Index("ix_rv_referrer_grade",        "referrer",    "grade"),
        Index("ix_rv_month_has_deposit",     "month",       "has_deposit"),
        Index("ix_rv_expected_start_label",  "expected_start_label"),
    )


class RecruitmentMonth(Base):
    """手動登記的招生月份（補充訪視記錄中未出現的月份）"""
    __tablename__ = "recruitment_months"

    id         = Column(Integer, primary_key=True, index=True)
    month      = Column(String(10), nullable=False, unique=True)  # 民國月份，如 "115.04"
    created_at = Column(DateTime, default=datetime.now)


class RecruitmentPeriod(Base):
    """近五年招生期間轉換整合表（每期半年一筆）"""
    __tablename__ = "recruitment_periods"

    id = Column(Integer, primary_key=True, index=True)
    period_name = Column(String(50), nullable=False, unique=True)   # 如 "114.09.16~115.03.15"
    visit_count = Column(Integer, default=0)                        # 參觀人數
    deposit_count = Column(Integer, default=0)                      # 預繳人數
    enrolled_count = Column(Integer, default=0)                     # 實際註冊人數
    transfer_term_count = Column(Integer, default=0)                # 轉到其他學期
    effective_deposit_count = Column(Integer, default=0)            # 有效預繳（預繳 - 轉期）
    not_enrolled_deposit = Column(Integer, default=0)               # 未就讀退預繳
    enrolled_after_school = Column(Integer, default=0)              # 註冊後退學
    notes = Column(Text, nullable=True)                             # 備註
    sort_order = Column(Integer, default=0)                         # 排序
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
