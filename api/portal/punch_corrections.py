"""
Portal - punch correction request endpoints（員工補打卡申請）
"""

import calendar as cal_module
from datetime import date, datetime, timedelta
from utils.taipei_time import today_taipei
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel, field_validator, model_validator

import logging

from models.approval import ApprovalStatus
from models.database import get_session, PunchCorrectionRequest, User
from utils.auth import get_current_user
from utils.permissions import Permission, list_active_user_ids_with_permission
from ._shared import _get_employee

logger = logging.getLogger(__name__)

router = APIRouter()


def _list_active_users_with_permission(session, perm: str) -> list[int]:
    """列出 permission_names 含 perm 的 active user_id（SQLite/PG 通用）。"""
    return list_active_user_ids_with_permission(session, perm)


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
        if self.attendance_date and self.attendance_date > today_taipei():
            raise ValueError("補打卡日期不得為未來日期")
        # requested_punch_in 的日期成分須等於 attendance_date
        if self.requested_punch_in is not None and self.attendance_date is not None:
            if self.requested_punch_in.date() != self.attendance_date:
                raise ValueError("申請上班時間的日期須與補打卡日期相同")
        # requested_punch_out 須等於 attendance_date，或跨夜 punch_out 允許隔日
        if self.requested_punch_out is not None and self.attendance_date is not None:
            allowed_out_dates = {
                self.attendance_date,
                self.attendance_date + timedelta(days=1),
            }
            if self.requested_punch_out.date() not in allowed_out_dates:
                raise ValueError("申請下班時間的日期須與補打卡日期相同（跨夜可為隔日）")
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
        "correction_type_label": CORRECTION_TYPE_LABELS.get(
            c.correction_type, c.correction_type
        ),
        "requested_punch_in": (
            c.requested_punch_in.isoformat() if c.requested_punch_in else None
        ),
        "requested_punch_out": (
            c.requested_punch_out.isoformat() if c.requested_punch_out else None
        ),
        "reason": c.reason,
        "approval_status": c.approval_status,
        "approved_by": c.approved_by,
        "rejection_reason": c.rejection_reason,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/my-punch-corrections")
def get_my_punch_corrections(
    year: Optional[int] = Query(None, ge=2000, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
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
        existing = (
            session.query(PunchCorrectionRequest)
            .filter(
                PunchCorrectionRequest.employee_id == emp.id,
                PunchCorrectionRequest.attendance_date == data.attendance_date,
                PunchCorrectionRequest.status
                != ApprovalStatus.REJECTED.value,  # pending 或 approved
            )
            .first()
        )
        if existing:
            status_label = (
                "待審核"
                if existing.status == ApprovalStatus.PENDING.value
                else "已核准"
            )
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
            status=ApprovalStatus.PENDING.value,
        )
        session.add(correction)
        session.flush()  # 取得 correction.id 供通知 context

        # 通知：commit 前 enqueue；對每位 APPROVALS reviewer 個人推送（對稱 leave /
        # overtime submit；補打卡 approve 端點守衛即 Permission.APPROVALS）。
        try:
            from services.notification import dispatch

            reviewer_user_ids = _list_active_users_with_permission(
                session, Permission.APPROVALS.value
            )
            for rid in reviewer_user_ids:
                dispatch.enqueue(
                    session=session,
                    event_type="punch_correction.submitted",
                    recipient_user_id=rid,
                    context={
                        "submitter_name": emp.name,
                        "target_date": data.attendance_date.isoformat(),
                        "correction_id": correction.id,
                    },
                    sender_id=current_user.get("user_id"),
                    source_entity_type="punch_correction_request",
                    source_entity_id=correction.id,
                )
        except Exception as exc:
            logger.warning("punch_correction.submitted enqueue 失敗（已吞）：%s", exc)

        session.commit()
        return {"message": "補打卡申請已送出，待主管核准", "id": correction.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
