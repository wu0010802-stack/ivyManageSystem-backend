"""
models/classroom.py — 班級、年級、學生模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Date, DateTime, Boolean, ForeignKey, Index, Text
from sqlalchemy.orm import relationship

from models.base import Base


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
    name = Column(String(50), unique=True, nullable=False)
    grade_id = Column(Integer, ForeignKey("class_grades.id"), nullable=True)
    capacity = Column(Integer, default=30)
    current_count = Column(Integer, default=0)

    head_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    assistant_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    art_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)

    class_code = Column(String(20), nullable=True, comment="班級代號")

    is_active = Column(Boolean, default=True)

    grade = relationship("ClassGrade")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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
    status = Column(String(20), nullable=True)

    parent_name = Column(String(50), nullable=True)
    parent_phone = Column(String(20), nullable=True)
    address = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True)
    status_tag = Column(String(50), nullable=True, comment="狀態標籤")

    __table_args__ = (
        Index('ix_student_classroom', 'classroom_id', 'is_active'),
    )

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
