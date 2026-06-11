"""懲處記錄 model

警告 / 小過 / 大過：扣減節慶+超額獎金（下一個發放期一次抵扣後標記已用）。
嘉獎 / 小功 / 大功（merit 類型）：獎勵紀錄（考核加分用），不參與薪資扣款。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money

ACTION_TYPE_WARNING = "warning"  # 警告
ACTION_TYPE_MINOR = "minor"  # 小過
ACTION_TYPE_MAJOR = "major"  # 大過
ACTION_TYPE_COMMEND = "commendation"  # 嘉獎
ACTION_TYPE_MINOR_MERIT = "minor_merit"  # 小功
ACTION_TYPE_MAJOR_MERIT = "major_merit"  # 大功

ACTION_TYPES = (
    ACTION_TYPE_WARNING,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_MAJOR,
    ACTION_TYPE_COMMEND,
    ACTION_TYPE_MINOR_MERIT,
    ACTION_TYPE_MAJOR_MERIT,
)

ACTION_TYPE_LABELS = {
    ACTION_TYPE_WARNING: "警告",
    ACTION_TYPE_MINOR: "小過",
    ACTION_TYPE_MAJOR: "大過",
    ACTION_TYPE_COMMEND: "嘉獎",
    ACTION_TYPE_MINOR_MERIT: "小功",
    ACTION_TYPE_MAJOR_MERIT: "大功",
}


class DisciplinaryAction(Base):
    """員工懲處記錄。

    抵扣語意：每筆懲處在「下一個獎金發放月」一次性從節慶+超額獎金扣減，
    扣完即寫入 applied_to_salary_id + applied_at + applied_amount。若獎金不足
    抵扣，applied_amount < deduction_amount，剩餘額度不滾入下次（業主慣例）。
    """

    __tablename__ = "disciplinary_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )

    action_date = Column(Date, nullable=False, comment="懲處發生日")
    action_type = Column(
        String(20),
        nullable=False,
        comment=(
            "warning=警告 / minor=小過 / major=大過（懲處，扣薪資） / "
            "commendation=嘉獎 / minor_merit=小功 / major_merit=大功（merit，僅考核加分不扣薪）"
        ),
    )
    deduction_amount = Column(
        Money, nullable=False, default=0, comment="扣款金額（0=用 BonusConfig 預設）"
    )
    reason = Column(Text, nullable=True, comment="懲處原因")

    # 抵扣狀態
    applied_to_salary_id = Column(
        Integer,
        ForeignKey("salary_records.id", ondelete="SET NULL"),
        nullable=True,
        comment="已抵扣的薪資 record id；NULL=尚未抵扣",
    )
    applied_at = Column(DateTime, nullable=True)
    applied_amount = Column(
        Money,
        nullable=True,
        comment="實際抵扣金額（可能 < deduction_amount 因獎金不足）",
    )

    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    created_by = Column(String(50), nullable=True)
    updated_at = Column(
        DateTime, default=now_taipei_naive, onupdate=now_taipei_naive, nullable=False
    )
    updated_by = Column(String(50), nullable=True)

    employee = relationship("Employee", lazy="select")

    __table_args__ = (
        Index(
            "ix_disciplinary_actions_employee_date",
            "employee_id",
            "action_date",
        ),
        Index(
            "ix_disciplinary_actions_pending",
            "employee_id",
            "applied_to_salary_id",
        ),
    )
