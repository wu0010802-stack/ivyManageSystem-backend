"""
models/student_log.py — 學生異動紀錄表

記錄每學期的入學、退學、轉出、轉入、畢業、復學事件，
支援自動寫入（create/graduate）與手動補登。
"""

from datetime import datetime, date

from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    DateTime,
    Text,
    ForeignKey,
    Index,
    func,
)

from models.base import Base

COMMUNICATION_TYPES = ["電話", "LINE", "面談", "Email", "家聯簿", "簡訊", "其他"]


class ParentCommunicationLog(Base):
    """家長溝通紀錄（電話、LINE、面談等）"""

    __tablename__ = "parent_communication_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    communication_date = Column(Date, nullable=False, comment="溝通日期")
    communication_type = Column(
        String(20), nullable=False, comment="溝通方式（電話/LINE/面談/Email/家聯簿...）"
    )
    topic = Column(String(100), nullable=True, comment="主題摘要")
    content = Column(Text, nullable=False, comment="溝通內容")
    follow_up = Column(Text, nullable=True, comment="後續追蹤事項")
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("ix_parent_comm_student", "student_id"),
        Index("ix_parent_comm_date", "communication_date"),
    )


class StudentChangeLog(Base):
    """學生異動紀錄"""

    __tablename__ = "student_change_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    school_year = Column(Integer, nullable=False)
    semester = Column(Integer, nullable=False)
    event_type = Column(String(20), nullable=False)  # 入學/復學/退學/轉出/轉入/畢業
    event_date = Column(Date, nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    from_classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    to_classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    reason = Column(String(50), nullable=True)  # 下拉選項值
    notes = Column(Text, nullable=True)  # 自由文字補充
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    source = Column(
        String(20),
        nullable=False,
        default="manual",
        server_default="manual",
        comment="來源：manual=手動補登；lifecycle=狀態機自動寫入（禁止編輯/刪除）",
    )
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        Index("ix_student_change_logs_student", "student_id"),
        Index("ix_student_change_logs_term", "school_year", "semester"),
        Index("ix_student_change_logs_event_date", "event_date"),
    )


# 各 event_type 對應的 reason 下拉選項
CHANGE_LOG_REASON_OPTIONS = {
    "入學": ["新生報名", "招生轉化", "其他"],
    "復學": ["復學", "其他"],
    "退學": ["家庭因素", "健康因素", "搬遷", "轉往他園", "其他"],
    "轉出": ["家庭因素", "健康因素", "搬遷", "轉往他園", "其他"],
    "轉入": ["從他園轉入", "其他"],
    "畢業": ["正常畢業"],
    "休學": ["家庭因素", "健康因素", "其他"],
}

EVENT_TYPES = list(CHANGE_LOG_REASON_OPTIONS.keys())


# 生命週期狀態 → event_type 對照（給 StudentLifecycleService 用）
LIFECYCLE_TO_EVENT_TYPE = {
    "prospect_converted": "入學",  # 招生轉化 → 正式學生
    "activated": "入學",  # enrolled → active（開學）
    "on_leave": "休學",
    "returned": "復學",  # on_leave → active
    "transferred": "轉出",
    "withdrawn": "退學",
    "graduated": "畢業",
}
