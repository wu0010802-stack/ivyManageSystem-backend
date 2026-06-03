"""一鍵離職主入口：單一 transaction 串接 4 step（Phase 1）。

設計參考：docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md §5
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from utils.taipei_time import now_taipei_naive, today_taipei
from typing import Literal, TypedDict

from sqlalchemy.orm import Session

from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord

logger = logging.getLogger(__name__)


class StepResult(TypedDict):
    step: str
    status: Literal["completed", "skipped", "failed"]
    completed_at: datetime | None
    payload: dict | None
    error: str | None


class OffboardingResult(TypedDict):
    employee_id: int
    resign_date: date
    is_active_after: bool
    user_account_revoked: bool
    steps: list[StepResult]
    certificate_pdf_path: str | None  # Phase 2 才填，Phase 1 一律 None


class OffboardingError(Exception):
    """離職流程錯誤。code 對應 API HTTP detail。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def process_offboarding(
    session: Session,
    employee_id: int,
    resign_date: date,
    resign_reason: str | None,
    operator_user_id: int,
) -> OffboardingResult:
    """一鍵離職主入口。

    Args:
        session: SQLAlchemy session（呼叫端負責 commit / rollback）
        employee_id: 對象員工 id
        resign_date: 離職日（可 > today 為通知期）
        resign_reason: 離職原因（不寫入證明 PDF）
        operator_user_id: 操作 admin User.id（寫入 opened_by_user_id）

    Returns:
        OffboardingResult dict

    Raises:
        OffboardingError: input 驗證失敗或任一 step 失敗；呼叫端必須 rollback session
    """
    emp = session.query(Employee).filter_by(id=employee_id).first()
    if emp is None:
        raise OffboardingError("員工不存在", code="EMPLOYEE_NOT_FOUND")

    existing = (
        session.query(EmployeeOffboardingRecord)
        .filter_by(employee_id=employee_id)
        .first()
    )
    if existing is not None:
        raise OffboardingError(
            f"員工 {employee_id} 已有離職紀錄 (resign_date={existing.resign_date})",
            code="ALREADY_OFFBOARDED",
        )

    if emp.hire_date and resign_date < emp.hire_date:
        raise OffboardingError(
            f"resign_date {resign_date} 早於 hire_date {emp.hire_date}",
            code="RESIGN_DATE_BEFORE_HIRE",
        )

    today = today_taipei()
    if (resign_date - today).days > 90:
        raise OffboardingError(
            f"resign_date {resign_date} 超過 today + 90 天",
            code="RESIGN_DATE_TOO_FAR_FUTURE",
        )

    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        resign_reason=resign_reason,
        opened_at=now_taipei_naive(),
        opened_by_user_id=operator_user_id,
    )
    session.add(record)
    session.flush()  # 取得 FK，但不 commit

    # 寫入 Employee.resign_date / resign_reason
    emp.resign_date = resign_date
    emp.resign_reason = resign_reason
    if resign_date <= today:
        emp.is_active = False

    # 離職觸發全 unfinalized SalaryRecord stale：proration / daily_wage 改動
    # 影響任何未封存月（保留 api/employees.py:520 原 endpoint 行為）。
    # 注意：prefill_salary step 也會標當月 stale，這裡是 superset 不衝突。
    from api.employees import _mark_employee_salary_stale

    _mark_employee_salary_stale(session, employee_id)

    from services.offboarding.steps import (
        generate_certificate,
        mark_appraisal,
        revoke_user,
        snapshot_leave,
    )

    steps_result: list[StepResult] = []
    user_account_revoked = False

    try:
        # Step 1: mark_appraisal
        steps_result.append(mark_appraisal.run(session, record))

        # Step 2: snapshot_leave
        steps_result.append(snapshot_leave.run(session, record))

        # Step 3: prefill_leave_payout（同模組 prefill_salary）
        steps_result.append(snapshot_leave.prefill_salary(session, record))

        # Step 4: revoke_user
        revoke_result = revoke_user.run(session, record)
        steps_result.append(revoke_result)
        if revoke_result["status"] == "completed" and revoke_result["payload"].get(
            "username"
        ):
            user_account_revoked = True

        # Step 5: generate_certificate（Phase 2）
        cert_result = generate_certificate.run(session, record)
        steps_result.append(cert_result)

        # Step 6: 偵測仍掛該員工的 active 班級導師綁定（標記待改派，不清空——
        # 直接 NULL 會讓 active 班級瞬間無導師；交由 HR 改派）。
        from services.offboarding.homeroom_check import (
            detect_dangling_homeroom_assignments,
        )

        dangling = detect_dangling_homeroom_assignments(session, employee_id)
        if dangling:
            logger.warning(
                "員工 %s 離職時仍為 %d 個 active 班級導師，需 HR 改派：%s",
                employee_id,
                len(dangling),
                dangling,
            )
        steps_result.append(
            StepResult(
                step="homeroom_reassignment_check",
                status="completed",
                completed_at=now_taipei_naive(),
                payload={"dangling_classrooms": dangling},
                error=None,
            )
        )

    except OffboardingError:
        raise  # 由 endpoint 層 catch + session.rollback

    return OffboardingResult(
        employee_id=employee_id,
        resign_date=resign_date,
        is_active_after=emp.is_active,
        user_account_revoked=user_account_revoked,
        steps=steps_result,
        certificate_pdf_path=record.certificate_pdf_path,  # Phase 2 起填入
    )
