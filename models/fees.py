"""
models/fees.py — 學費/費用管理資料模型
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

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
    JSON,
    CheckConstraint,
    text,
)

from models.base import Base

FEE_TYPE_REGISTRATION = "registration"
FEE_TYPE_MISCELLANEOUS = "miscellaneous"
FEE_TYPE_MONTHLY = "monthly"
FEE_TYPE_MATERIAL = "material"
FEE_TYPE_INSURANCE = "insurance"
FEE_TYPE_CUSTOM = "custom"

FEE_TYPES_TEMPLATE = (
    FEE_TYPE_REGISTRATION,
    FEE_TYPE_MISCELLANEOUS,
    FEE_TYPE_MONTHLY,
    FEE_TYPE_MATERIAL,
    FEE_TYPE_INSURANCE,
)

FEE_TYPES_ALL = FEE_TYPES_TEMPLATE + (FEE_TYPE_CUSTOM,)


class FeeTemplate(Base):
    """費用範本：以「年級×學年×學期×費用類型」為唯一鍵，
    驅動批次產生學期費用記錄。"""

    __tablename__ = "fee_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grade_id = Column(
        Integer,
        ForeignKey("class_grades.id", ondelete="RESTRICT"),
        nullable=False,
    )
    school_year = Column(Integer, nullable=False, comment="民國年")
    semester = Column(Integer, nullable=False, comment="1=上, 2=下")
    fee_type = Column(
        String(20),
        nullable=False,
        comment="registration / miscellaneous / monthly",
    )
    name = Column(String(100), nullable=False)
    amount = Column(Integer, nullable=False)
    breakdown = Column(
        JSON,
        nullable=True,
        comment="月費組成 e.g. {tuition:8500, meal:3000, transport:1500}",
    )
    due_date_offset_days = Column(Integer, default=14, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(50), nullable=True)
    updated_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "grade_id",
            "school_year",
            "semester",
            "fee_type",
            name="uq_fee_template",
        ),
        CheckConstraint(
            "fee_type IN ("
            "'registration','miscellaneous','monthly','material','insurance',"
            "'tuition','transport','summer_uniform','summer_sports'"
            ")",
            name="ck_fee_template_type",
        ),
        CheckConstraint("amount >= 0", name="ck_fee_template_amount_nonneg"),
        CheckConstraint("semester IN (1, 2)", name="ck_fee_template_semester"),
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

    fee_type = Column(
        String(20),
        nullable=True,
        comment="費用類型(registration/miscellaneous/monthly/material/insurance/custom)",
    )
    source_template_id = Column(
        Integer,
        ForeignKey("fee_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_month = Column(
        String(7),
        nullable=True,
        comment="僅 monthly 使用,格式 YYYY-MM",
    )

    period = Column(
        String(20), nullable=False, comment="學年學期（denormalized，便於篩選）"
    )
    due_date = Column(
        Date,
        nullable=True,
        comment="繳費期限；家長端可分類「即將到期」與「已逾期」",
    )

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        # c2: uq_student_fee_item 已 DROP（fee_item_id 變 nullable 後唯一鍵失效）
        # c3: fee_item_id column 已 DROP；monthly 冪等改靠 ix_fee_records_monthly_unique
        Index("ix_fee_records_period_status", "period", "status"),
        Index("ix_fee_records_student", "student_id"),
        Index("ix_fee_records_student_period", "student_id", "period"),
        Index("ix_fee_records_due_date", "due_date"),
        Index("ix_fee_records_fee_type", "fee_type"),
        # 非月費（target_month IS NULL）冪等鍵：同 (學生, 範本, 學期) 只能一張，
        # 阻擋並發 generate 雙寫註冊費 / 制服費 / 學費。
        # 月費另由 ix_fee_records_monthly_unique 處理。
        Index(
            "uq_fee_records_non_monthly_unique",
            "student_id",
            "source_template_id",
            "period",
            unique=True,
            postgresql_where=text(
                "source_template_id IS NOT NULL AND target_month IS NULL"
            ),
            sqlite_where=text(
                "source_template_id IS NOT NULL AND target_month IS NULL"
            ),
        ),
    )


class StudentFeePayment(Base):
    """學費繳費流水：每次收款 append 一筆，不再覆寫 StudentFeeRecord。

    Why: 舊設計 StudentFeeRecord 只保留單一 amount_paid/payment_date/status，
    分期收款會覆寫；月報過濾 `status='paid' + payment_date in month` 會把
    多期收款全部搬到最後一次付款的月份、退款後 partial 狀態整筆消失、
    partial 現金不入帳。改走 append-only 流水即可正確聚合月度收入。

    與 StudentFeeRefund 對稱：退款仍走獨立表，兩者分別計算淨額。
    """

    __tablename__ = "student_fee_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(
        Integer,
        ForeignKey("student_fee_records.id", ondelete="RESTRICT"),
        nullable=False,
        comment="對應的學生費用記錄",
    )
    amount = Column(Integer, nullable=False, comment="本次收款金額（正整數）")
    payment_date = Column(Date, nullable=False, comment="本次收款日期")
    payment_method = Column(
        String(20), nullable=True, comment="繳費方式：現金/轉帳/其他"
    )
    notes = Column(Text, nullable=True, default="", comment="備註")
    operator = Column(String(50), nullable=True, comment="操作人員 username")
    # 冪等鍵：網路重送時同 key 視為重試，避免雙扣（NULL 允許重複，相容舊資料）
    idempotency_key = Column(
        String(64), nullable=True, comment="繳費冪等鍵（全域唯一）"
    )
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (
        Index("ix_fee_payments_record", "record_id"),
        Index("ix_fee_payments_date", "payment_date"),
        Index("ix_fee_payments_record_date", "record_id", "payment_date"),
        Index("ix_fee_payments_idk", "idempotency_key"),
        UniqueConstraint("idempotency_key", name="uq_student_fee_payments_idk"),
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
    refunded_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    calc_method = Column(
        String(30),
        nullable=True,
        comment="enrollment_ratio / monthly_partial / manual",
    )
    calc_payload = Column(
        JSON,
        nullable=True,
        comment="計算明細 e.g. {T_total:100,T_served:30,ratio:'<1/3',refund_ratio:'2/3'}",
    )
    # 冪等鍵：網路重送時同 key 視為重試，避免重複退款（NULL 允許重複，相容舊資料）
    idempotency_key = Column(
        String(64), nullable=True, comment="退款冪等鍵（10 分鐘視窗內同 key 視為重試）"
    )

    __table_args__ = (
        Index("ix_fee_refunds_record", "record_id"),
        Index("ix_fee_refunds_refunded_at", "refunded_at"),
        Index("ix_fee_refunds_idk_refunded", "idempotency_key", "refunded_at"),
        UniqueConstraint("idempotency_key", name="uq_student_fee_refunds_idk"),
    )


# adjustment_type 列舉（折抵類，從應收中扣除）
ADJUSTMENT_TYPE_SIBLING_DISCOUNT = "sibling_discount"  # 同胞優惠
ADJUSTMENT_TYPE_PREPAYMENT = "prepayment"  # 預繳折抵
ADJUSTMENT_TYPE_LEAVE_DEDUCTION = "leave_deduction"  # 請假扣款
ADJUSTMENT_TYPE_OTHER = "other"  # 其他


class StudentFeeAdjustment(Base):
    """學費折抵：同胞優惠 / 預繳 / 請假扣款 等「減少應收」的記錄。

    為何獨立成表而非以負金額的 StudentFeeRecord 表達：
    - StudentFeeRecord.amount_due 有 ≥ 1 守衛，整套 payment/refund 審計與
      月度聚合都假設正金額；硬塞負值會破壞既有不變式
    - 折抵需追蹤獨立「原因」（同胞、提前繳款、請假天數等）

    應用方式：
    - 該生該學期 total_due = SUM(records.amount_due) - SUM(adjustments.amount)
    - 不影響 amount_paid 與支付流水
    - 同一學生同學期可有多筆同 type 折抵（不加 UNIQUE）
    """

    __tablename__ = "student_fee_adjustments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        comment="對應學生",
    )
    period = Column(String(20), nullable=False, comment="學期，如 114-2")
    adjustment_type = Column(
        String(50),
        nullable=False,
        comment="sibling_discount/prepayment/leave_deduction/other",
    )
    amount = Column(
        Integer,
        nullable=False,
        comment="折抵金額（正整數，套用時相減）",
    )
    reason = Column(String(200), nullable=True, comment="折抵原因說明")
    notes = Column(Text, nullable=True, default="", comment="備註")
    created_by = Column(String(50), nullable=True, comment="建立者 username")
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        Index("ix_fee_adjustments_student_period", "student_id", "period"),
        Index("ix_fee_adjustments_type", "adjustment_type"),
        CheckConstraint("amount > 0", name="ck_fee_adjustments_amount_positive"),
    )
