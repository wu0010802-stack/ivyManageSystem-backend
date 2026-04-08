"""
models/event.py — 假日、會議、活動、公告模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from models.base import Base


class Holiday(Base):
    """國定假日表"""
    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False, comment="日期")
    name = Column(String(100), nullable=False, comment="假日名稱")
    is_active = Column(Boolean, default=True)
    description = Column(String(200), nullable=True)
    source = Column(String(20), nullable=True, comment="資料來源，例如 dgpa/manual")
    source_year = Column(Integer, nullable=True, comment="同步來源年份")
    synced_at = Column(DateTime, nullable=True, comment="最後同步時間")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_holidays_source_year", "source", "source_year"),
    )


class WorkdayOverride(Base):
    """補班日 / 工作日覆蓋表"""
    __tablename__ = "workday_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False, comment="覆蓋日期")
    name = Column(String(100), nullable=False, comment="顯示名稱")
    description = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    source = Column(String(20), nullable=True, comment="資料來源，例如 dgpa")
    source_year = Column(Integer, nullable=True, comment="同步來源年份")
    synced_at = Column(DateTime, nullable=True, comment="最後同步時間")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_workday_overrides_source_year", "source", "source_year"),
    )


class OfficialCalendarSync(Base):
    """官方年度日曆同步狀態"""
    __tablename__ = "official_calendar_syncs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_year = Column(Integer, unique=True, nullable=False, index=True)
    is_synced = Column(Boolean, default=False, nullable=False)
    used_cache = Column(Boolean, default=False, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    source = Column(String(20), nullable=False, default="dgpa")
    source_modified_at = Column(String(50), nullable=True, comment="來源端資源更新時間")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MeetingRecord(Base):
    """園務會議記錄表"""
    __tablename__ = "meeting_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    meeting_date = Column(Date, nullable=False, comment="會議日期")
    meeting_type = Column(String(30), default="staff_meeting", comment="會議類型: staff_meeting")
    attended = Column(Boolean, default=True, comment="是否出席")
    overtime_hours = Column(Float, default=0, comment="加班時數")
    overtime_pay = Column(Float, default=0, comment="加班費")

    remark = Column(Text, comment="備註")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_meeting_emp_date', 'employee_id', 'meeting_date'),
        Index('ix_meeting_date_attended', 'meeting_date', 'attended'),
    )

    employee = relationship("Employee", backref="meeting_records")


class SchoolEvent(Base):
    """學校行事曆事件表"""
    __tablename__ = "school_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, comment="事件標題")
    description = Column(Text, comment="事件說明")
    event_date = Column(Date, nullable=False, comment="事件日期")
    end_date = Column(Date, comment="結束日期（多日事件）")
    event_type = Column(String(30), default="general", comment="事件類型: meeting/activity/holiday/general")
    is_all_day = Column(Boolean, default=True, comment="是否全天")
    start_time = Column(String(5), comment="開始時間 HH:MM")
    end_time = Column(String(5), comment="結束時間 HH:MM")
    location = Column(String(100), comment="地點")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Announcement(Base):
    """公告表"""
    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_created_at", "created_at"),
        Index("ix_announcements_created_by", "created_by"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, comment="公告標題")
    content = Column(Text, nullable=False, comment="公告內容")
    priority = Column(String(20), default="normal", comment="優先級: normal/important/urgent")
    is_pinned = Column(Boolean, default=False, comment="是否置頂")
    created_by = Column(Integer, ForeignKey("employees.id"), nullable=False, comment="發佈者")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    author = relationship("Employee", backref="announcements")
    recipients = relationship("AnnouncementRecipient", backref="announcement", cascade="all, delete-orphan", lazy="select")


class AnnouncementRead(Base):
    """公告已讀記錄表"""
    __tablename__ = "announcement_reads"
    __table_args__ = (
        UniqueConstraint("announcement_id", "employee_id", name="uq_announcement_employee"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    read_at = Column(DateTime, default=datetime.now, comment="閱讀時間")

    announcement = relationship("Announcement", backref="reads")


class AnnouncementRecipient(Base):
    """公告指定對象表（空代表全員可見）"""
    __tablename__ = "announcement_recipients"
    __table_args__ = (
        UniqueConstraint("announcement_id", "employee_id", name="uq_ann_recipient"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
