"""才藝老師薪資明細 model。

每月每老師可有多筆給付（不同科目/班級/星期），對齊《義華薪資》才藝老師 sheet。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money


class ArtTeacherPayrollEntry(Base):
    """才藝老師單筆薪資明細。

    語意：一個 hourly 員工某月可有 N 筆（如 Vadim 4 月 = 外師 + 課後美語(二)）。
    薪資引擎在計算時，若 employee_type='hourly' 且該月有 entries，
    salary_record.hourly_total / net_salary 將以 sum(entries.total_amount) 覆寫。
    """

    __tablename__ = "art_teacher_payroll_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    salary_year = Column(Integer, nullable=False)
    salary_month = Column(Integer, nullable=False)

    subject = Column(String(50), nullable=False, comment="科目")
    classroom_label = Column(String(50), nullable=True, comment="班級/星期備註")

    hours = Column(Float, nullable=False, default=0)
    hourly_rate = Column(Money, nullable=False, default=0)
    base_amount = Column(Money, nullable=False, default=0, comment="hours × rate")

    excess_amount = Column(Money, nullable=False, default=0, comment="超額加給")
    activity_bonus = Column(Money, nullable=False, default=0, comment="加給活動")

    total_amount = Column(
        Money, nullable=False, default=0, comment="base+excess+activity"
    )

    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    created_by = Column(String(50), nullable=True)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
    updated_by = Column(String(50), nullable=True)

    employee = relationship("Employee", lazy="select")

    __table_args__ = (
        Index(
            "ix_art_teacher_payroll_emp_month",
            "employee_id",
            "salary_year",
            "salary_month",
        ),
    )
