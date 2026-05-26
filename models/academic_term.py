"""models/academic_term.py — 學年/學期/開學日設定。

scheduler 在 `start_date` 當天觸發批量推進 enrolled → active。
is_current 用於 admin 顯式翻牌、partial unique 保證 singleton。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive
from sqlalchemy import (
    Column,
    Integer,
    Boolean,
    Date,
    DateTime,
    UniqueConstraint,
    CheckConstraint,
    Index,
    text,
)
from models.base import Base


class AcademicTerm(Base):
    __tablename__ = "academic_terms"

    id = Column(Integer, primary_key=True, index=True)
    school_year = Column(Integer, nullable=False, comment="民國學年")
    semester = Column(Integer, nullable=False, comment="1=上學期、2=下學期")
    start_date = Column(Date, nullable=False, comment="開學日")
    end_date = Column(Date, nullable=False)
    is_current = Column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
        comment="目前學期旗標；全表至多一筆 true",
    )
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        UniqueConstraint(
            "school_year", "semester", name="uq_academic_terms_year_semester"
        ),
        CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        CheckConstraint("semester IN (1, 2)", name="ck_academic_terms_semester_valid"),
        Index(
            "uq_academic_terms_is_current_singleton",
            "is_current",
            unique=True,
            postgresql_where=text("is_current = true"),
            sqlite_where=text("is_current = 1"),
        ),
    )
