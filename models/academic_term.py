"""models/academic_term.py — 學年/學期/開學日設定。

scheduler 在 `start_date` 當天觸發批量推進 enrolled → active。
"""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    Date,
    DateTime,
    UniqueConstraint,
    CheckConstraint,
)
from models.base import Base


class AcademicTerm(Base):
    __tablename__ = "academic_terms"

    id = Column(Integer, primary_key=True, index=True)
    school_year = Column(Integer, nullable=False, comment="民國學年")
    semester = Column(Integer, nullable=False, comment="1=上學期、2=下學期")
    start_date = Column(Date, nullable=False, comment="開學日")
    end_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "school_year", "semester", name="uq_academic_terms_year_semester"
        ),
        CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        CheckConstraint("semester IN (1, 2)", name="ck_academic_terms_semester_valid"),
    )
