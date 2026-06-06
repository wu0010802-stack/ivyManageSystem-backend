"""revoke_user step：抽自 api/employees.py:783-806。

resign_date <= today：User.is_active=False + token_version+=1（已簽發 cookie 立刻失效）
resign_date > today：通知期保留 User active；當日 cron 自動轉
"""

import logging
from datetime import date, datetime
from utils.taipei_time import now_taipei_naive, today_taipei

from sqlalchemy.orm import Session

from models.auth import User
from models.offboarding import EmployeeOffboardingRecord
from models.staff_refresh_token import StaffRefreshToken
from services.offboarding.orchestrator import StepResult

logger = logging.getLogger(__name__)


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    today = today_taipei()
    now = now_taipei_naive()
    if record.resign_date > today:
        return {
            "step": "revoke_user",
            "status": "skipped",
            "completed_at": now,
            "payload": {"reason": "notice_period"},
            "error": None,
        }

    user = (
        session.query(User)
        .filter(
            User.employee_id == record.employee_id,
            User.is_active.is_(True),
        )
        .first()
    )

    if user is None:
        record.user_revoked_at = now
        return {
            "step": "revoke_user",
            "status": "completed",
            "completed_at": now,
            "payload": {"username": None, "note": "no_active_user"},
            "error": None,
        }

    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    # R6-4：同 transaction 撤銷該 user 所有 staff_refresh family（比照 change_password /
    # update_user）。is_active=False 雖已讓 /refresh 401（即時失效），但未撤的 family 是
    # 死資料；若日後「重新啟用員工」未 bump token_version 會復活舊 cookie。inline 撤避免
    # revoke_all_for_user 另開 session 脫離本離職 transaction。
    session.query(StaffRefreshToken).filter(
        StaffRefreshToken.user_id == user.id,
        StaffRefreshToken.revoked_at.is_(None),
    ).update({"revoked_at": now}, synchronize_session=False)
    record.user_revoked_at = now

    logger.warning(
        "員工 %s 離職撤 User 帳號：username=%s token_version 升至 %d",
        record.employee_id,
        user.username,
        user.token_version,
    )

    return {
        "step": "revoke_user",
        "status": "completed",
        "completed_at": now,
        "payload": {"username": user.username, "new_token_version": user.token_version},
        "error": None,
    }
