"""
Punch correction management router（管理員審核補打卡申請）
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_

from models.database import get_session, Employee, Attendance, PunchCorrectionRequest, User, ApprovalPolicy, ApprovalLog
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["punch-corrections"])

def _get_submitter_role(employee_id: int, session) -> str:
    user = session.query(User).filter(
        User.employee_id == employee_id,
        User.is_active == True,
    ).first()
    return user.role if user else "teacher"


def _check_approval_eligibility(doc_type: str, submitter_role: str, approver_role: str, session) -> bool:
    policy = session.query(ApprovalPolicy).filter(
        ApprovalPolicy.is_active == True,
        ApprovalPolicy.submitter_role == submitter_role,
        ApprovalPolicy.doc_type.in_([doc_type, "all"]),
    ).first()
    if not policy:
        return approver_role == "admin"
    return approver_role in [r.strip() for r in policy.approver_roles.split(",")]


def _write_approval_log(doc_type: str, doc_id: int, action: str, approver: dict, comment: str | None, session):
    session.add(ApprovalLog(
        doc_type=doc_type,
        doc_id=doc_id,
        action=action,
        approver_id=approver.get("id"),
        approver_username=approver.get("username", ""),
        approver_role=approver.get("role", ""),
        comment=comment,
    ))


CORRECTION_TYPE_LABELS = {
    "punch_in": "補上班打卡",
    "punch_out": "補下班打卡",
    "both": "補全天打卡",
}


class ApproveRequest(BaseModel):
    approved: bool
    rejection_reason: Optional[str] = None


def _format_correction(c: PunchCorrectionRequest, employee_name: str = "") -> dict:
    return {
        "id": c.id,
        "employee_id": c.employee_id,
        "employee_name": employee_name,
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


@router.get("/punch-corrections")
def list_punch_corrections(
    status: Optional[str] = Query(None, description="pending / approved / rejected"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    employee_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_permission(Permission.APPROVALS)),
):
    """查詢補打卡申請（管理員用）"""
    session = get_session()
    try:
        query = session.query(PunchCorrectionRequest, Employee).join(
            Employee, PunchCorrectionRequest.employee_id == Employee.id
        )

        if status == "pending":
            query = query.filter(PunchCorrectionRequest.is_approved.is_(None))
        elif status == "approved":
            query = query.filter(PunchCorrectionRequest.is_approved.is_(True))
        elif status == "rejected":
            query = query.filter(PunchCorrectionRequest.is_approved.is_(False))

        if year and month:
            import calendar as cal_module
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            query = query.filter(
                PunchCorrectionRequest.attendance_date >= start,
                PunchCorrectionRequest.attendance_date <= end,
            )

        if employee_id:
            query = query.filter(PunchCorrectionRequest.employee_id == employee_id)

        rows = query.order_by(PunchCorrectionRequest.created_at.desc()).all()
        return [_format_correction(c, emp.name) for c, emp in rows]
    finally:
        session.close()


@router.put("/punch-corrections/{correction_id}/approve")
def approve_punch_correction(
    correction_id: int,
    body: ApproveRequest,
    current_user: dict = Depends(require_permission(Permission.APPROVALS)),
):
    """核准或駁回補打卡申請"""
    session = get_session()
    try:
        correction = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.id == correction_id
        ).first()
        if not correction:
            raise HTTPException(status_code=404, detail="找不到此補打卡申請")

        if correction.is_approved is not None:
            status_label = "已核准" if correction.is_approved else "已駁回"
            raise HTTPException(status_code=400, detail=f"此申請已{status_label}，無法再次審核")

        # ── 角色資格檢查 ──────────────────────────────────────────────────────
        submitter_role = _get_submitter_role(correction.employee_id, session)
        approver_role = current_user.get("role", "")
        if not _check_approval_eligibility("punch_correction", submitter_role, approver_role, session):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的補打卡申請",
            )

        if not body.approved:
            # 駁回
            if not body.rejection_reason or not body.rejection_reason.strip():
                raise HTTPException(status_code=422, detail="駁回時必須填寫駁回原因")
            correction.is_approved = False
            correction.rejection_reason = body.rejection_reason.strip()
            correction.approved_by = current_user.get("username", "")
            _write_approval_log("punch_correction", correction_id, "rejected", current_user,
                                body.rejection_reason, session)
            session.commit()
            logger.warning(
                "補打卡申請 #%d（員工 %d，日期 %s）已由 %s 駁回",
                correction_id, correction.employee_id,
                correction.attendance_date, current_user.get("username"),
            )
            return {"message": "補打卡申請已駁回"}

        # 核准：取得或建立 Attendance 記錄
        att = session.query(Attendance).filter(
            Attendance.employee_id == correction.employee_id,
            Attendance.attendance_date == correction.attendance_date,
        ).first()
        if not att:
            att = Attendance(
                employee_id=correction.employee_id,
                attendance_date=correction.attendance_date,
            )
            session.add(att)

        if correction.correction_type in ("punch_in", "both"):
            att.punch_in_time = correction.requested_punch_in
        if correction.correction_type in ("punch_out", "both"):
            att.punch_out_time = correction.requested_punch_out

        # 重算缺打卡旗標
        att.is_missing_punch_in = (att.punch_in_time is None)
        att.is_missing_punch_out = (att.punch_out_time is None)

        # 更新申請狀態
        correction.is_approved = True
        correction.approved_by = current_user.get("username", "")
        _write_approval_log("punch_correction", correction_id, "approved", current_user, None, session)
        session.commit()

        logger.warning(
            "補打卡申請 #%d（員工 %d，日期 %s）已由 %s 核准",
            correction_id, correction.employee_id,
            correction.attendance_date, current_user.get("username"),
        )
        return {"message": "補打卡申請已核准，考勤記錄已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error("核准補打卡申請 #%d 時發生錯誤：%s", correction_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
