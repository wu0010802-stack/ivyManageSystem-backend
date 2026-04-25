"""models/student_leave.py — 學生請假申請（家長端發起，教師審核）

業務規則（plan A.4）：
- approve 必在同一 transaction 對 start_date..end_date 的「應到日」upsert
  StudentAttendance，approval wins；衝突時保留原 recorded_by 與 remark 前綴
- reject / cancel 反向清除 StudentAttendance（僅清 remark 前綴吻合者）
- 「應到日」靠 services/workday_rules.classify_day 判定（排除 holiday 與
  weekend，但保留 makeup 補班日 — 學生補班日要到校）
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from models.base import Base


# 與 StudentAttendance.status 共用語意（中文字串）
LEAVE_TYPES = ("病假", "事假")
LEAVE_STATUSES = ("pending", "approved", "rejected", "cancelled")


class StudentLeaveRequest(Base):
    """家長端發起的學生請假申請。"""

    __tablename__ = "student_leave_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    applicant_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        comment="家長 User",
    )
    applicant_guardian_id = Column(
        Integer,
        ForeignKey("guardians.id", ondelete="SET NULL"),
        nullable=True,
        comment="申請當下的 Guardian（軟刪也保留歷史）",
    )
    leave_type = Column(String(10), nullable=False, comment="病假/事假")
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
    attachment_path = Column(
        String(255), nullable=True, comment="家長上傳的證明檔（診斷證明等）"
    )
    status = Column(
        String(15),
        nullable=False,
        default="pending",
        comment="pending/approved/rejected/cancelled",
    )
    reviewed_by = Column(
        Integer, ForeignKey("users.id"), nullable=True, comment="審核者 User"
    )
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_slr_student_daterange",
            "student_id",
            "start_date",
            "end_date",
        ),
        Index("ix_slr_status_created", "status", "created_at"),
    )
