"""term.changed subscriber：跨學年（下→新學年上）為員工生 leave_quotas new row。

行為矩陣：
- old=None：跳過 + log info
- same school_year (1→2)：no-op + log info（quota 不按學期切換、按學年）
- 跨學年（X-2 → X+1-1）：為每位 active 員工 INSERT new row with school_year=X+1
  - annual: 依 hire_date → new_term.start_date 年資套勞基法第 38 條
  - QUOTA_LEAVE_TYPES 其他: STATUTORY_QUOTA_HOURS
  - compensatory: 結餘 carry-over (舊 row.total_hours - approved_used_in_old_term)
- 其他切換: no-op + log info
- idempotent: pre-check school_year row 已存在則 skip
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from api.leaves_quota import (
    QUOTA_LEAVE_TYPES,
    STATUTORY_QUOTA_HOURS,
    _calc_annual_leave_hours,
)
from models.academic_term import AcademicTerm
from models.employee import Employee
from models.approval import ApprovalStatus
from models.leave import LeaveQuota, LeaveRecord
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
    """為每位 active 員工 INSERT 6 種假別 new leave_quotas row with school_year=new.school_year。"""
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
            if lt in existing_types:
                continue
            if lt == "annual":
                hours = _calc_annual_leave_hours(
                    emp.hire_date,
                    year=annual_ref.year,
                    reference_date=annual_ref,
                )
                note = f"年資 (基準 {annual_ref.isoformat()}) 換算（依勞基法第38條）"
            else:
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
    """補休結餘 = 上學年 row.total_hours - 已核准已用 (篩選 old term 區間)。

    Cold-start 相容：first toggle 時系統內只有 legacy year-only row。
    先按 school_year 查、找不到 fallback 找 (school_year IS NULL AND year=old.start_date.year)
    的 legacy row。避免全員 silently 歸零。
    """
    # 學年 row 優先
    old_quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year == old.school_year,
            LeaveQuota.leave_type == "compensatory",
        )
        .first()
    )
    # Cold-start fallback：legacy year-only row
    if not old_quota:
        old_quota = (
            session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.school_year.is_(None),
                LeaveQuota.year == old.start_date.year,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
    if not old_quota:
        return 0.0
    approved_used = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == "compensatory",
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.start_date >= old.start_date,
            LeaveRecord.start_date < new.start_date,
        )
        .scalar()
        or 0.0
    )
    return max(0.0, float(old_quota.total_hours) - float(approved_used))
