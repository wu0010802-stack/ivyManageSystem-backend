"""term.changed subscriber：跨學年（下→新學年上）為員工生 leave_quotas new row。

行為矩陣：
- old=None：跳過 + log info
- same school_year (1→2)：no-op + log info（quota 不按學期切換、按學年）
- 跨學年（X-2 → X+1-1）：為每位 active 員工 INSERT new row with school_year=X+1
  - annual: 【不再建】改由 anniversary scheduler (services/leave_quota_expiry/annual_cutover.py) 負責
  - QUOTA_LEAVE_TYPES 其他 (sick/menstrual/personal/family_care): STATUTORY_QUOTA_HOURS
  - compensatory: grant ledger SUM(granted_hours - consumed_hours) WHERE status='active' carry-over
- 其他切換: no-op + log info
- idempotent: pre-check school_year row 已存在則 skip
"""

import logging

from sqlalchemy.orm import Session

from api.leaves_quota import (
    QUOTA_LEAVE_TYPES,
    STATUTORY_QUOTA_HOURS,
)
from models.academic_term import AcademicTerm
from models.employee import Employee
from models.leave import LeaveQuota
from utils.term_events import on_term_changed

logger = logging.getLogger(__name__)


@on_term_changed("leave_quota_cutover")
def handle(*, old: AcademicTerm | None, new: AcademicTerm, session: Session) -> None:
    if old is None:
        logger.info("leave_quota_cutover: 初次設定 is_current，跳過 cutover")
        return

    if not (
        old.school_year + 1 == new.school_year
        and old.semester == 2
        and new.semester == 1
    ):
        logger.info(
            "leave_quota_cutover: 非跨學年切換 (%s-%s → %s-%s)，no-op",
            old.school_year,
            old.semester,
            new.school_year,
            new.semester,
        )
        return

    _cutover_for_all_active_employees(old, new, session)


def _cutover_for_all_active_employees(
    old: AcademicTerm, new: AcademicTerm, session: Session
) -> None:
    """為每位 active 員工 INSERT 法定假別（不含 annual）+ compensatory carry-over row。

    annual（特休）改由 anniversary scheduler 負責，cutover 不再建立。
    """
    active_emps = session.query(Employee).filter(Employee.is_active.is_(True)).all()

    annual_ref = new.start_date
    new_sy = new.school_year

    created_count = 0
    for emp in active_emps:
        existing_types = {
            r[0]
            for r in (
                session.query(LeaveQuota.leave_type)
                .filter(
                    LeaveQuota.employee_id == emp.id,
                    LeaveQuota.school_year == new_sy,
                )
                .all()
            )
        }

        for lt in QUOTA_LEAVE_TYPES:
            if lt == "annual":
                continue  # 特休改由 anniversary scheduler 處理（services/leave_quota_expiry/annual_cutover.py）
            if lt in existing_types:
                continue
            hours = STATUTORY_QUOTA_HOURS[lt]
            note = "法定年度上限（學年制）"

            session.add(
                LeaveQuota(
                    employee_id=emp.id,
                    year=annual_ref.year,  # legacy 欄保留同年（供舊 caller 過渡）
                    school_year=new_sy,
                    leave_type=lt,
                    total_hours=hours,
                    note=note,
                )
            )
            created_count += 1

        # 補休（不在 QUOTA_LEAVE_TYPES，但要 carry-over 結餘）
        if "compensatory" not in existing_types:
            balance = _calc_compensatory_balance(emp.id, old, new, session)
            session.add(
                LeaveQuota(
                    employee_id=emp.id,
                    year=annual_ref.year,
                    school_year=new_sy,
                    leave_type="compensatory",
                    total_hours=balance,
                    note=f"上學年結餘 {balance:.1f} 小時 carry-over",
                )
            )
            created_count += 1

    session.flush()
    logger.info(
        "leave_quota_cutover: %d 位員工生 %d 筆 leave_quotas row (school_year=%s)",
        len(active_emps),
        created_count,
        new_sy,
    )


def _calc_compensatory_balance(
    employee_id: int,
    old: AcademicTerm,
    new: AcademicTerm,
    session: Session,
) -> float:
    """補休結餘改用 grant ledger SUM 作 source of truth（T8 helper）。

    舊版查 LeaveQuota.compensatory.total_hours - approved_used 的 cache 路徑作廢。
    grant ledger 直接 SUM(granted_hours - consumed_hours) WHERE status='active'。

    old/new params 保留以維持 caller 相容，不使用。
    """
    from services.leave_quota_expiry.helpers import _compensatory_balance

    return _compensatory_balance(employee_id, session)
