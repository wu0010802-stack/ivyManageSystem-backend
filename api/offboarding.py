"""員工離職 checklist API endpoint（Phase 1）。

Phase 1 提供：preview / process / get / nhi-unenroll
Phase 2 補：certificate.pdf / magic-link / download
Phase 3 補：list
"""

import io
import logging
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from models.auth import User
from models.database import get_session
from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from models.salary import SalaryRecord
from schemas.offboarding import (
    AppraisalInFlightCycle,
    LeaveSnapshotPreview,
    MagicLinkResponse,
    MagicLinkRevokeResponse,
    NhiUnenrollRequest,
    OffboardingDetailResponse,
    OffboardingPreview,
    OffboardingPreviewRequest,
    OffboardingPreviewResponse,
    OffboardingProcessRequest,
    OffboardingProcessResponse,
    SalaryRecordTarget,
    StepResultModel,
)
from services.offboarding.download_bundle import build_offboarding_zip
from services.offboarding.magic_link import (
    generate_token as ml_generate_token,
    is_active as _is_magic_link_active,
    record_download as ml_record_download,
    revoke_token as ml_revoke_token,
    verify_token as ml_verify_token,
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
        operator_user_id = current_user["user_id"]
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
        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = (
            f"離職處理：employee/{employee_id} resign_date={result['resign_date']} "
            f"steps_completed={[s['step'] for s in result['steps'] if s['status'] == 'completed']}"
        )

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


@router.get("/download")
def download_offboarding_bundle(token: str, request: Request):
    """**公開無 auth** download endpoint。

    以 magic-link token 串流 ZIP 離職包（離職證明 PDF + 12 月薪資 PDF + 出勤 CSV）。
    驗失敗統一 410 Gone，不暴露差異原因（防 enumeration）。

    Security headers：
    - Content-Disposition: attachment（強制下載，不在瀏覽器 inline render）
    - X-Content-Type-Options: nosniff（防 MIME sniffing）
    - Cache-Control: no-store（代理 / CDN 不快取含 PII 的 ZIP）

    TODO follow-up：uvicorn access log token redaction（ASGI middleware 攔 query string
    或 --access-log False）。目前 endpoint 本身不 echo token 到 logger，但 uvicorn
    預設 access log 會記完整 URL（含 ?token=...），建議後續 PR 加 ASGI middleware
    做 query string sanitize 或改用 --access-log False。
    """
    session: Session = get_session()
    try:
        record = ml_verify_token(session, token)
        if record is None:
            raise HTTPException(status_code=410, detail="LINK_NO_LONGER_VALID")

        zip_bytes = build_offboarding_zip(session, record)
        ml_record_download(session, record)

        # audit log（公開 endpoint 仍記，但不 echo token）
        request.state.audit_entity_id = str(record.employee_id)
        request.state.audit_summary = (
            f"離職 ZIP 下載：employee/{record.employee_id} "
            f"count={record.magic_link_download_count}"
        )

        session.commit()

        from urllib.parse import quote

        emp = session.query(Employee).filter_by(id=record.employee_id).first()
        emp_name = emp.name if emp else "employee"
        utf8_filename = (
            f"ivy-offboarding-{emp_name}-{record.resign_date.isoformat()}.zip"
        )
        # RFC 5987：ASCII fallback + UTF-8 percent-encoded（HTTP header 不能含 non-latin1）
        ascii_filename = (
            f"ivy-offboarding-{record.employee_id}-{record.resign_date.isoformat()}.zip"
        )
        content_disposition = (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(utf8_filename)}"
        )

        logger.info(
            "離職 ZIP 下載：employee_id=%s count=%s size=%d bytes",
            record.employee_id,
            record.magic_link_download_count,
            len(zip_bytes),
        )

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={
                "Content-Disposition": content_disposition,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    finally:
        session.close()


@router.get("/{employee_id}", response_model=OffboardingDetailResponse)
def get_offboarding_detail(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    """取得員工離職 checklist 完整紀錄。

    gated by EMPLOYEES_READ；record 不存在回 404 OFFBOARDING_RECORD_NOT_FOUND。
    """
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="OFFBOARDING_RECORD_NOT_FOUND")

        emp = session.query(Employee).filter_by(id=employee_id).first()
        return OffboardingDetailResponse(
            employee_id=record.employee_id,
            employee_name=emp.name if emp else "",
            resign_date=record.resign_date,
            resign_reason=record.resign_reason,
            opened_at=record.opened_at,
            opened_by_user_id=record.opened_by_user_id,
            appraisal_marked_at=record.appraisal_marked_at,
            leave_snapshot_at=record.leave_snapshot_at,
            user_revoked_at=record.user_revoked_at,
            certificate_generated_at=record.certificate_generated_at,
            leave_balance_snapshot=record.leave_balance_snapshot,
            certificate_pdf_path=record.certificate_pdf_path,
            nhi_unenroll_submitted_at=record.nhi_unenroll_submitted_at,
            magic_link_active=_is_magic_link_active(record),
            magic_link_expires_at=record.magic_link_expires_at,
            magic_link_download_count=record.magic_link_download_count or 0,
            magic_link_last_used_at=record.magic_link_last_used_at,
            closed_at=record.closed_at,
        )
    finally:
        session.close()


@router.get("/{employee_id}/certificate.pdf")
def get_certificate_pdf(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    """admin 取離職證明 PDF。

    gated by EMPLOYEES_READ；record 不存在或 certificate_pdf_path 空
    → 404 CERTIFICATE_NOT_FOUND；檔案不存在 → 404 CERTIFICATE_FILE_MISSING。
    """
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None or not record.certificate_pdf_path:
            raise HTTPException(status_code=404, detail="CERTIFICATE_NOT_FOUND")

        pdf_path = Path(record.certificate_pdf_path)
        if not pdf_path.exists():
            raise HTTPException(status_code=404, detail="CERTIFICATE_FILE_MISSING")

        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename=pdf_path.name,
        )
    finally:
        session.close()


@router.post("/{employee_id}/magic-link", response_model=MagicLinkResponse)
def post_magic_link(
    employee_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """admin 產 magic-link token（30 天 / 3 次上限）。覆寫舊 hash（重發即作廢前一個）。"""
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(404, "OFFBOARDING_RECORD_NOT_FOUND")
        token = ml_generate_token(session, record)
        session.commit()

        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = (
            f"離職 magic-link 產生：employee/{employee_id} "
            f"expires_at={record.magic_link_expires_at.isoformat()}"
        )

        return MagicLinkResponse(
            employee_id=employee_id,
            token=token,
            expires_at=record.magic_link_expires_at,
            download_url=f"/api/offboarding/download?token={token}",
        )
    finally:
        session.close()


@router.delete("/{employee_id}/magic-link", response_model=MagicLinkRevokeResponse)
def delete_magic_link(
    employee_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """admin 撤 magic-link token。"""
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(404, "OFFBOARDING_RECORD_NOT_FOUND")
        if record.magic_link_token_hash is None:
            raise HTTPException(404, "NO_ACTIVE_MAGIC_LINK")
        ml_revoke_token(session, record)
        session.commit()

        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = f"離職 magic-link 撤銷：employee/{employee_id}"

        return MagicLinkRevokeResponse(
            employee_id=employee_id,
            revoked_at=record.magic_link_revoked_at,
        )
    finally:
        session.close()


@router.patch("/{employee_id}/nhi-unenroll")
def patch_nhi_unenroll(
    employee_id: int,
    req: NhiUnenrollRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """手動標記全民健保退保申報狀態。

    submitted=true → 設 nhi_unenroll_submitted_at = now()
    submitted=false → 清空（設 None）
    gated by EMPLOYEES_WRITE。
    """
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="OFFBOARDING_RECORD_NOT_FOUND")

        record.nhi_unenroll_submitted_at = datetime.now() if req.submitted else None
        session.commit()

        # audit log（正確 key：middleware 讀 audit_entity_id / audit_summary）
        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = (
            f"健保退保標記：employee/{employee_id} submitted={req.submitted}"
        )

        return {
            "employee_id": employee_id,
            "nhi_unenroll_submitted_at": record.nhi_unenroll_submitted_at,
        }
    finally:
        session.close()
