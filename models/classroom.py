"""
models/classroom.py — 班級、年級、學生模型
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship


from models.base import Base
from utils.academic import resolve_current_academic_term


# ============ 學生生命週期狀態 ============
# 狀態機定義（合法轉移見 services/student_lifecycle.py）
LIFECYCLE_PROSPECT = "prospect"          # 招生訪視中，尚未報到
LIFECYCLE_ENROLLED = "enrolled"          # 已繳訂金/報到，尚未開學
LIFECYCLE_ACTIVE = "active"              # 正式在學
LIFECYCLE_ON_LEAVE = "on_leave"          # 休學
LIFECYCLE_TRANSFERRED = "transferred"    # 已轉出他園（終態）
LIFECYCLE_WITHDRAWN = "withdrawn"        # 退學（終態，可復學）
LIFECYCLE_GRADUATED = "graduated"        # 畢業（終態）

LIFECYCLE_STATUSES = [
    LIFECYCLE_PROSPECT,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    LIFECYCLE_GRADUATED,
]


def _default_school_year() -> int:
    school_year, _ = resolve_current_academic_term()
    return school_year


def _default_semester() -> int:
    _, semester = resolve_current_academic_term()
    return semester


class ClassGrade(Base):
    """年級表"""

    __tablename__ = "class_grades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    age_range = Column(String(20), nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Classroom(Base):
    """班級表"""

    __tablename__ = "classrooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    school_year = Column(
        Integer,
        nullable=False,
        default=_default_school_year,
        comment="學年度（起始年）",
    )
    semester = Column(
        Integer,
        nullable=False,
        default=_default_semester,
        comment="學期：1=上學期(8-1), 2=下學期(2-7)",
    )
    grade_id = Column(Integer, ForeignKey("class_grades.id"), nullable=True)
    capacity = Column(Integer, default=30)

    head_teacher_id = Column(
        Integer, ForeignKey("employees.id"), nullable=True, index=True
    )
    assistant_teacher_id = Column(
        Integer, ForeignKey("employees.id"), nullable=True, index=True
    )
    art_teacher_id = Column(
        Integer, ForeignKey("employees.id"), nullable=True, index=True
    )

    class_code = Column(String(20), nullable=True, comment="班級代號")

    is_active = Column(Boolean, default=True)

    grade = relationship("ClassGrade")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "school_year", "semester", "name", name="uq_classrooms_term_name"
        ),
        Index("ix_classrooms_term_active", "school_year", "semester", "is_active"),
        Index("ix_classroom_is_active", "is_active"),
    )


class Student(Base):
    """學生表"""

    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(String(20), unique=True, nullable=False, comment="學號")
    name = Column(String(50), nullable=False, comment="姓名")
    gender = Column(String(10), nullable=True)
    birthday = Column(Date, nullable=True)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    enrollment_date = Column(Date, nullable=True)
    graduation_date = Column(Date, nullable=True)
    withdrawal_date = Column(Date, nullable=True, comment="退學/轉出日期")
    status = Column(String(20), nullable=True)
    lifecycle_status = Column(
        String(20),
        nullable=False,
        default=LIFECYCLE_ACTIVE,
        server_default=LIFECYCLE_ACTIVE,
        comment="生命週期狀態：prospect/enrolled/active/on_leave/transferred/withdrawn/graduated",
    )
    recruitment_visit_id = Column(
        Integer,
        ForeignKey("recruitment_visits.id", ondelete="SET NULL"),
        nullable=True,
        comment="對應的招生訪視記錄（若透過 convert 流程建立）",
    )

    parent_name = Column(String(50), nullable=True, comment="[deprecated] 用 guardians 表；相容期保留快照")
    parent_phone = Column(String(20), nullable=True, comment="[deprecated] 用 guardians 表；相容期保留快照")
    address = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True)
    status_tag = Column(String(50), nullable=True, comment="狀態標籤")

    # 健康資訊
    allergy = Column(Text, nullable=True, comment="過敏原")
    medication = Column(Text, nullable=True, comment="用藥說明")
    special_needs = Column(Text, nullable=True, comment="特殊需求")

    # 緊急聯絡人（第二聯絡人）
    emergency_contact_name = Column(String(50), nullable=True, comment="緊急聯絡人姓名")
    emergency_contact_phone = Column(
        String(20), nullable=True, comment="緊急聯絡人電話"
    )
    emergency_contact_relation = Column(String(20), nullable=True, comment="與學生關係")

    __table_args__ = (
        Index("ix_student_classroom", "classroom_id", "is_active"),
        Index("ix_student_enrollment_grad", "enrollment_date", "graduation_date"),
        Index("ix_student_lifecycle_status", "lifecycle_status"),
    )

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class StudentIncident(Base):
    """學生事件紀錄表"""

    __tablename__ = "student_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    incident_type = Column(
        String(20), nullable=False
    )  # 身體健康 / 意外受傷 / 行為觀察 / 其他
    severity = Column(String(10), nullable=True)  # 輕微 / 中度 / 嚴重
    occurred_at = Column(DateTime, nullable=False)
    description = Column(Text, nullable=False)
    action_taken = Column(Text, nullable=True)
    parent_notified = Column(Boolean, default=False)
    parent_notified_at = Column(DateTime, nullable=True)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_student_incidents_student", "student_id"),
        Index("ix_student_incidents_date", "occurred_at"),
    )


class StudentAttendance(Base):
    """學生出席紀錄表"""

    __tablename__ = "student_attendances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    date = Column(Date, nullable=False)
    # 出席 / 缺席 / 病假 / 事假 / 遲到
    status = Column(String(10), nullable=False, default="出席")
    remark = Column(String(200), nullable=True)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("student_id", "date", name="uq_student_attendance_date"),
        Index("ix_student_attendance_date", "date"),
        Index("ix_student_attendance_student", "student_id"),
    )


class StudentAssessment(Base):
    """學生學期評量記錄表"""

    __tablename__ = "student_assessments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    semester = Column(String(20), nullable=False)  # e.g. "2025上" / "2025下"
    assessment_type = Column(String(20), nullable=False)  # 期中 / 期末 / 學期
    domain = Column(
        String(30), nullable=True
    )  # 身體動作與健康/語文/認知/社會/情緒/美感/綜合
    rating = Column(String(10), nullable=True)  # 優 / 良 / 需加強
    content = Column(Text, nullable=False)  # 評量觀察內容
    suggestions = Column(Text, nullable=True)  # 改善建議
    assessment_date = Column(Date, nullable=False)  # 評量日期
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_student_assessments_student", "student_id"),
        Index("ix_student_assessments_semester", "semester"),
    )
