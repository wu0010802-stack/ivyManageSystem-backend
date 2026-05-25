"""員工離職 checklist API endpoint（Phase 1）。

Phase 1 提供：preview / process / get / nhi-unenroll
Phase 2 補：certificate.pdf / magic-link / download
Phase 3 補：list
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.auth import User
from models.database import get_session
from models.employee import Employee
from models.salary import SalaryRecord
from schemas.offboarding import (
    AppraisalInFlightCycle,
    LeaveSnapshotPreview,
    OffboardingPreview,
    OffboardingPreviewRequest,
    OffboardingPreviewResponse,
    OffboardingProcessRequest,
    OffboardingProcessResponse,
    SalaryRecordTarget,
    StepResultModel,
)
from services.offboarding.orchestrator import OffboardingError, process_offboarding
from services.offboarding.steps.snapshot_leave import _resolve_daily_wage
from utils.auth import require_staff_permission
from utils.leave_quota_helpers import get_annual_leave_balance
from utils.permissions import Permission

_ERROR_TO_STATUS: dict[str, int] = {
    "EMPLOYEE_NOT_FOUND": 404,
    "ALREADY_OFFBOARDED": 409,
    "RESIGN_DATE_BEFORE_HIRE": 400,
    "RESIGN_DATE_TOO_FAR_FUTURE": 400,
    "LEAVE_BALANCE_NOT_FOUND": 422,
    "CERTIFICATE_GENERATION_FAILED": 500,
}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/offboarding", tags=["offboarding"])


@router.post("/{employee_id}/preview", response_model=OffboardingPreviewResponse)
def preview_offboarding(
    employee_id: int,
    req: OffboardingPreviewRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """預覽離職將執行的動作（純讀，不寫 DB）。"""
    session: Session = get_session()
    try:
        emp = session.query(Employee).filter_by(id=employee_id).first()
        if emp is None:
            raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")

        # 計算特休餘額
        balance = get_annual_leave_balance(session, employee_id, req.resign_date)

        # 計算日薪（從 snapshot_leave step 共用邏輯）
        daily_wage = _resolve_daily_wage(emp)
        payout = round(balance["remaining_days"] * (daily_wage or 0), 2)

        # 查離職當月薪資記錄
        sr = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == employee_id,
                SalaryRecord.salary_year == req.resign_date.year,
                SalaryRecord.salary_month == req.resign_date.month,
            )
            .first()
        )

        # 查有無 active User 帳號
        today = date.today()
        user_active = (
            session.query(User)
            .filter(
                User.employee_id == employee_id,
                User.is_active.is_(True),
            )
            .first()
        )

        # appraisal in-flight：Phase 1 簡化回空 list
        # Task 12 aggregator filter 改後自動含；preview 顯示僅作 hint
        in_flight_cycles: list[AppraisalInFlightCycle] = []

        # 組 warnings
        warnings: list[str] = []
        if not daily_wage:
            warnings.append("員工無 daily_wage / monthly_salary，特休折現無法計算")
        if in_flight_cycles:
            warnings.append(
                f"員工有 {len(in_flight_cycles)} 個進行中考核 cycle，"
                "標旗後仍保留於評議名單需 admin 人工結算"
            )

        return OffboardingPreviewResponse(
            employee_id=employee_id,
            employee_name=emp.name,
            resign_date=req.resign_date,
            preview=OffboardingPreview(
                user_account_will_be_revoked=(
                    req.resign_date <= today and user_active is not None
                ),
                leave_snapshot=LeaveSnapshotPreview(
                    special_leave_days=balance["remaining_days"],
                    daily_wage=float(daily_wage or 0),
                    payout_amount=payout,
                ),
                salary_record_target=SalaryRecordTarget(
                    year=req.resign_date.year,
                    month=req.resign_date.month,
                    exists=sr is not None,
                    will_be_marked_stale=sr is not None,
                ),
                appraisal_in_flight_cycles=in_flight_cycles,
                certificate_pdf_ready_to_generate=False,  # Phase 2 才實作
            ),
            warnings=warnings,
        )
    finally:
        session.close()


@router.post("/{employee_id}/process", response_model=OffboardingProcessResponse)
def process_offboarding_endpoint(
    employee_id: int,
    req: OffboardingProcessRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """一鍵離職主處理 endpoint。

    串接 orchestrator 的 4 step（mark_appraisal / snapshot_leave /
    prefill_leave_payout / revoke_user），失敗時 rollback 並依
    _ERROR_TO_STATUS 映射回 4xx/5xx。
    """
    session: Session = get_session()
    try:
        operator_user_id = int(current_user.get("user_id") or current_user.get("sub"))
        try:
            result = process_offboarding(
                session=session,
                employee_id=employee_id,
                resign_date=req.resign_date,
                resign_reason=req.resign_reason,
                operator_user_id=operator_user_id,
            )
        except OffboardingError as e:
            session.rollback()
            status = _ERROR_TO_STATUS.get(e.code, 500)
            raise HTTPException(status_code=status, detail=e.code)

        session.commit()

        # audit log（既有 middleware pattern：在 request.state 記）
        request.state.audit_action = "OFFBOARDING_PROCESSED"
        request.state.audit_target = f"employee/{employee_id}"
        request.state.audit_meta = {
            "resign_date": str(result["resign_date"]),
            "steps_completed": [
                s["step"] for s in result["steps"] if s["status"] == "completed"
            ],
        }

        logger.warning(
            "離職處理完成：employee_id=%s resign_date=%s operator=%s",
            employee_id,
            result["resign_date"],
            current_user.get("username"),
        )

        return OffboardingProcessResponse(
            employee_id=employee_id,
            resign_date=result["resign_date"],
            is_active=result["is_active_after"],
            user_account_revoked=result["user_account_revoked"],
            steps=[StepResultModel(**s) for s in result["steps"]],
            certificate_download_url=None,  # Phase 2 才補
        )
    finally:
        session.close()
