"""
models/activity.py — 課後才藝報名系統資料模型
"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Text,
    ForeignKey, UniqueConstraint, Index,
)

from models.base import Base


class ActivityCourse(Base):
    """才藝課程"""
    __tablename__ = "activity_courses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, comment="課程名稱")
    price = Column(Integer, nullable=False, comment="價格（元）")
    sessions = Column(Integer, nullable=True, comment="堂數")
    capacity = Column(Integer, default=30, comment="容量上限")
    video_url = Column(Text, nullable=True, comment="介紹影片 URL")
    allow_waitlist = Column(Boolean, default=True, comment="允許候補")
    description = Column(Text, nullable=True, comment="說明")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ActivitySupply(Base):
    """學員用品"""
    __tablename__ = "activity_supplies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, comment="用品名稱")
    price = Column(Integer, nullable=False, comment="價格（元）")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ActivityRegistration(Base):
    """報名主表"""
    __tablename__ = "activity_registrations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_name = Column(String(50), nullable=False, comment="學生姓名")
    birthday = Column(String(20), nullable=True, comment="生日（YYYY-MM-DD）")
    # 班級以字串儲存，避免刪班影響歷史紀錄
    class_name = Column(String(50), nullable=True, comment="班級名稱（字串）")
    email = Column(String(200), nullable=True, comment="聯絡信箱")
    is_paid = Column(Boolean, default=False, comment="是否已繳費（衍生欄位，paid_amount >= total）")
    paid_amount = Column(Integer, default=0, nullable=False, comment="累計已繳金額")
    remark = Column(Text, nullable=True, comment="備註")
    is_active = Column(Boolean, default=True, comment="軟刪除旗標")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_activity_registrations_active', 'is_active'),
        Index('ix_activity_regs_active_created', 'is_active', 'created_at'),
        Index('ix_activity_regs_paid', 'is_paid'),
        Index('ix_activity_regs_active_paid', 'is_active', 'is_paid'),
    )


class RegistrationCourse(Base):
    """報名課程關聯"""
    __tablename__ = "registration_courses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(
        Integer,
        ForeignKey("activity_registrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    course_id = Column(
        Integer,
        ForeignKey("activity_courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    # enrolled / waitlist
    status = Column(String(20), nullable=False, default="enrolled", comment="狀態")
    price_snapshot = Column(Integer, default=0, comment="報名時價格快照")

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("registration_id", "course_id", name="uq_reg_course"),
        Index('ix_reg_courses_status', 'course_id', 'status'),
        Index('ix_reg_course_reg_status', 'registration_id', 'course_id', 'status'),
        Index('ix_reg_courses_course_reg', 'course_id', 'registration_id', 'status'),
        # 候補排位查詢用：按 (course_id, status) 過濾後以 id 排序，找最早候補
        Index('ix_reg_courses_waitlist_order', 'course_id', 'status', 'id'),
    )


class RegistrationSupply(Base):
    """報名用品關聯"""
    __tablename__ = "registration_supplies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(
        Integer,
        ForeignKey("activity_registrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    supply_id = Column(
        Integer,
        ForeignKey("activity_supplies.id", ondelete="CASCADE"),
        nullable=False,
    )
    price_snapshot = Column(Integer, default=0, comment="報名時價格快照")

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("registration_id", "supply_id", name="uq_reg_supply"),
        Index('ix_reg_supply_reg', 'registration_id'),
    )


class ParentInquiry(Base):
    """家長提問"""
    __tablename__ = "parent_inquiries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, comment="家長姓名")
    phone = Column(String(30), nullable=False, comment="聯絡電話")
    question = Column(Text, nullable=False, comment="問題內容")
    is_read = Column(Boolean, default=False, comment="是否已讀")
    reply = Column(Text, nullable=True)
    replied_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_parent_inquiries_is_read', 'is_read'),
    )


class RegistrationChange(Base):
    """修改紀錄追蹤"""
    __tablename__ = "registration_changes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(
        Integer,
        ForeignKey("activity_registrations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 冗餘姓名：即使報名被刪除，紀錄仍可讀
    student_name = Column(String(50), nullable=False, comment="學生姓名（冗餘）")
    change_type = Column(String(50), nullable=False, comment="變更類型")
    description = Column(Text, nullable=False, comment="描述")
    changed_by = Column(String(100), nullable=True, comment="操作者帳號")

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_registration_changes_reg', 'registration_id'),
    )


class ActivityRegistrationSettings(Base):
    """報名開放設定（singleton，只有一列）"""
    __tablename__ = "activity_registration_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    is_open = Column(Boolean, default=False, comment="是否開放報名")
    open_at = Column(String(50), nullable=True, comment="開放時間（ISO string）")
    close_at = Column(String(50), nullable=True, comment="截止時間（ISO string）")

    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ActivityPaymentRecord(Base):
    """才藝報名繳費／退費明細記錄"""
    __tablename__ = "activity_payment_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration_id = Column(
        Integer,
        ForeignKey("activity_registrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(String(10), nullable=False, default="payment",
                  comment="payment（繳費）/ refund（退費）")
    amount = Column(Integer, nullable=False, comment="金額（永遠為正整數）")
    payment_date = Column(Date, nullable=False, comment="繳費/退費日期")
    payment_method = Column(String(20), nullable=True, comment="現金/轉帳/其他")
    notes = Column(Text, nullable=True, comment="備註")
    operator = Column(String(50), nullable=True, comment="操作人員帳號")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_activity_payment_records_reg", "registration_id"),
    )


class ActivitySession(Base):
    """才藝課程場次（某課程某天的一次上課）"""
    __tablename__ = "activity_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    course_id = Column(
        Integer,
        ForeignKey("activity_courses.id", ondelete="CASCADE"),
        nullable=False,
        comment="課程 ID",
    )
    session_date = Column(Date, nullable=False, comment="上課日期")
    notes = Column(Text, nullable=True, comment="場次備註")
    created_by = Column(String(100), nullable=True, comment="建立者帳號")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("course_id", "session_date", name="uq_activity_session_course_date"),
        Index("ix_activity_sessions_course_id", "course_id"),
        Index("ix_activity_sessions_date", "session_date"),
    )


class ActivityAttendance(Base):
    """才藝課程點名記錄（場次 × 學生出席狀態）"""
    __tablename__ = "activity_attendances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("activity_sessions.id", ondelete="CASCADE"),
        nullable=False,
        comment="場次 ID",
    )
    registration_id = Column(
        Integer,
        ForeignKey("activity_registrations.id", ondelete="CASCADE"),
        nullable=False,
        comment="報名 ID",
    )
    is_present = Column(Boolean, nullable=False, default=True, comment="是否出席")
    notes = Column(Text, nullable=True, comment="備註")
    recorded_by = Column(String(100), nullable=True, comment="點名者帳號")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("session_id", "registration_id", name="uq_activity_attendance_session_reg"),
        Index("ix_activity_attendances_session_id", "session_id"),
        Index("ix_activity_attendances_reg_id", "registration_id"),
        Index("ix_activity_attendances_session_present", "session_id", "is_present"),
    )
