"""
Portal - punch correction request endpoints（員工補打卡申請）
"""

import calendar as cal_module
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator

from models.database import get_session, PunchCorrectionRequest
from utils.auth import get_current_user
from ._shared import _get_employee

router = APIRouter()

CORRECTION_TYPE_LABELS = {
    "punch_in": "補上班打卡",
    "punch_out": "補下班打卡",
    "both": "補全天打卡",
}


class PunchCorrectionCreate(BaseModel):
    attendance_date: date
    correction_type: str
    requested_punch_in: Optional[datetime] = None
    requested_punch_out: Optional[datetime] = None
    reason: Optional[str] = None

    @field_validator("correction_type")
    @classmethod
    def validate_correction_type(cls, v):
        if v not in CORRECTION_TYPE_LABELS:
            allowed = ", ".join(CORRECTION_TYPE_LABELS.keys())
            raise ValueError(f"無效的補正類型，允許值：{allowed}")
        return v

    @model_validator(mode="after")
    def validate_required_times(self):
        if self.attendance_date and self.attendance_date > date.today():
            raise ValueError("補打卡日期不得為未來日期")
        if self.correction_type == "punch_in" and not self.requested_punch_in:
            raise ValueError("補正類型為「補上班打卡」時，申請上班時間為必填")
        if self.correction_type == "punch_out" and not self.requested_punch_out:
            raise ValueError("補正類型為「補下班打卡」時，申請下班時間為必填")
        if self.correction_type == "both":
            if not self.requested_punch_in:
                raise ValueError("補正類型為「補全天打卡」時，申請上班時間為必填")
            if not self.requested_punch_out:
                raise ValueError("補正類型為「補全天打卡」時，申請下班時間為必填")
        return self


def _format_correction(c: PunchCorrectionRequest) -> dict:
    return {
        "id": c.id,
        "attendance_date": c.attendance_date.isoformat(),
        "correction_type": c.correction_type,
        "correction_type_label": CORRECTION_TYPE_LABELS.get(c.correction_type, c.correction_type),
        "requested_punch_in": c.requested_punch_in.isoformat() if c.requested_punch_in else None,
        "requested_punch_out": c.requested_punch_out.isoformat() if c.requested_punch_out else None,
        "reason": c.reason,
        "approval_status": c.approval_status,
        "approved_by": c.approved_by,
        "rejection_reason": c.rejection_reason,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/my-punch-corrections")
def get_my_punch_corrections(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """取得個人補打卡申請記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        query = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.employee_id == emp.id,
        )

        if year and month:
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            query = query.filter(
                PunchCorrectionRequest.attendance_date >= start,
                PunchCorrectionRequest.attendance_date <= end,
            )

        records = query.order_by(PunchCorrectionRequest.created_at.desc()).all()
        return [_format_correction(r) for r in records]
    finally:
        session.close()


@router.post("/my-punch-corrections", status_code=201)
def create_my_punch_correction(
    data: PunchCorrectionCreate,
    current_user: dict = Depends(get_current_user),
):
    """提交補打卡申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        # 防重複：同員工同日期若已有待審或已核准的申請
        existing = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.employee_id == emp.id,
            PunchCorrectionRequest.attendance_date == data.attendance_date,
            PunchCorrectionRequest.is_approved.isnot(False),  # None 或 True
        ).first()
        if existing:
            status_label = "待審核" if existing.is_approved is None else "已核准"
            raise HTTPException(
                status_code=400,
                detail=f"您在 {data.attendance_date} 已有{status_label}的補打卡申請（ID: {existing.id}），請勿重複送出",
            )

        correction = PunchCorrectionRequest(
            employee_id=emp.id,
            attendance_date=data.attendance_date,
            correction_type=data.correction_type,
            requested_punch_in=data.requested_punch_in,
            requested_punch_out=data.requested_punch_out,
            reason=data.reason,
            is_approved=None,
        )
        session.add(correction)
        session.commit()
        return {"message": "補打卡申請已送出，待主管核准", "id": correction.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
