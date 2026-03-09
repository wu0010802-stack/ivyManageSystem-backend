"""
Portal shared constants, Pydantic models, and helper functions.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from models.database import (
    get_session, Employee, DailyShift, ShiftAssignment,
)
from utils.auth import get_current_user
from api.overtimes import MAX_OVERTIME_HOURS

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
    "compensatory": "補休",
}

OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}


# ============ Pydantic Models ============

class LeaveCreatePortal(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: float = 8
    reason: Optional[str] = None

    @field_validator("leave_hours")
    @classmethod
    def validate_leave_hours(cls, v):
        if v < 0.5:
            raise ValueError("請假時數至少 0.5 小時")
        if v > 480:
            raise ValueError("請假時數不得超過 480 小時")
        if round(v * 2) != v * 2:
            raise ValueError("請假時數必須為 0.5 小時的倍數")
        return v

    @model_validator(mode="after")
    def validate_date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("結束日期不得早於開始日期")
        if self.start_date and self.end_date and (
            self.start_date.year != self.end_date.year
            or self.start_date.month != self.end_date.month
        ):
            raise ValueError(
                "請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請"
                f"（本次 {self.start_date.year}/{self.start_date.month:02d} 月 →"
                f" {self.end_date.year}/{self.end_date.month:02d} 月）"
            )
        return self


class OvertimeCreatePortal(BaseModel):
    overtime_date: date
    overtime_type: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: float
    reason: Optional[str] = None
    use_comp_leave: bool = False  # 以補休代替加班費（1:1 換算）

    @field_validator("overtime_type")
    @classmethod
    def validate_overtime_type(cls, v):
        if v not in OVERTIME_TYPE_LABELS:
            allowed = ", ".join(OVERTIME_TYPE_LABELS.keys())
            raise ValueError(f"無效的加班類型，允許值：{allowed}")
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v):
        if v <= 0:
            raise ValueError("加班時數必須大於 0")
        if v > MAX_OVERTIME_HOURS:
            raise ValueError(f"單筆加班時數不得超過 {MAX_OVERTIME_HOURS:.0f} 小時")
        return v


class AnomalyConfirm(BaseModel):
    action: str  # "use_pto" | "accept" | "dispute"
    remark: Optional[str] = None


class ProfileUpdate(BaseModel):
    phone: Optional[str] = None
    address: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None


class SwapRequestCreate(BaseModel):
    target_id: int
    swap_date: date
    reason: Optional[str] = None


class SwapRequestRespond(BaseModel):
    action: str  # "accept" | "reject"
    remark: Optional[str] = None


# ============ Helpers ============

def _get_employee(session, current_user: dict) -> Employee:
    employee_id = current_user.get("employee_id")
    if not employee_id:
        raise HTTPException(
            status_code=403,
            detail="此帳號無關聯員工資料，請先使用身份切換功能進入前台",
        )
    emp = session.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="找不到對應的員工資料")
    return emp


def _get_employee_shift_for_date(session, employee_id: int, target_date: date):
    """取得員工在指定日期的班別（優先 DailyShift -> ShiftAssignment）"""
    # 1. DailyShift override
    ds = session.query(DailyShift).filter(
        DailyShift.employee_id == employee_id,
        DailyShift.date == target_date,
    ).first()
    if ds:
        return ds.shift_type_id

    # 2. Weekly ShiftAssignment
    week_monday = target_date - timedelta(days=target_date.weekday())
    sa = session.query(ShiftAssignment).filter(
        ShiftAssignment.employee_id == employee_id,
        ShiftAssignment.week_start_date == week_monday,
    ).first()
    if sa:
        return sa.shift_type_id

    return None


def _get_shift_type_map(session, active_only: bool = False) -> dict:
    """取得 ShiftType {id: obj} 對照表"""
    from models.database import ShiftType
    query = session.query(ShiftType)
    if active_only:
        query = query.filter(ShiftType.is_active == True)
    return {st.id: st for st in query.all()}


def _calculate_annual_leave_quota(hire_date: date) -> int:
    """
    根據勞基法計算特休天數 (週年制)
    """
    if not hire_date:
        return 0

    today = date.today()
    months_diff = (today.year - hire_date.year) * 12 + today.month - hire_date.month
    if today.day < hire_date.day:
        months_diff -= 1

    years = months_diff // 12

    if months_diff < 6:
        return 0
    elif 6 <= months_diff < 12:
        return 3
    elif 1 <= years < 2:
        return 7
    elif 2 <= years < 3:
        return 10
    elif 3 <= years < 5:
        return 14
    elif 5 <= years < 10:
        return 15
    else:
        extra_days = years - 10
        total = 15 + extra_days
        return min(total, 30)
