"""offboarding_revoke_scheduler：離職到期撤帳 enforcement（R6-3）。

未來日期離職（resign_date > today）時 revoke_user 回 skipped，User.is_active 維持
True；revoke_user docstring 宣稱「當日 cron 自動轉」但**原本無此 scheduler** → 已離職
員工在離職日後仍保有完整登入 + staff_refresh，直到有人手動撤。

此 scheduler 每 interval 掃 resign_date<=today 但仍未撤（user_revoked_at IS NULL）的
離職記錄，補執行 revoke_user。冪等：只處理 user_revoked_at IS NULL，多 worker 重跑
或處理已撤記錄皆無害。
"""

import asyncio
import logging

from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.taipei_time import today_taipei

logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    from config import get_settings

    return get_settings().scheduler.offboarding_revoke_enabled


def run_offboarding_revoke_due_once() -> dict:
    """掃 resign_date<=today 但 User 仍 active（user_revoked_at IS NULL）的離職記錄，
    補執行 revoke_user（is_active=False + token_version bump + 撤 staff_refresh family）。
    回 {"revoked": n, "failed": m}。冪等。

    C33：每筆獨立故障隔離。原本整批包單一交易，一筆 poison record 例外會 rollback
    全批 → 永久 head-of-line blocking（該筆每次巡檢都先被撈出又先炸，其後所有到期離職
    永遠撤不掉而監控全綠）。改為每筆用 begin_nested()(savepoint)，單筆例外只 rollback
    該 savepoint + log + 計入 failed，續處理其餘記錄。"""
    from models.base import session_scope
    from models.offboarding import EmployeeOffboardingRecord
    from services.offboarding.steps.revoke_user import run as revoke_run

    today = today_taipei()
    revoked = 0
    failed = 0
    with session_scope() as session:
        records = (
            session.query(EmployeeOffboardingRecord)
            .filter(
                EmployeeOffboardingRecord.resign_date <= today,
                EmployeeOffboardingRecord.user_revoked_at.is_(None),
            )
            .all()
        )
        for record in records:
            try:
                with session.begin_nested():
                    result = revoke_run(session, record)
                if result.get("status") == "completed":
                    revoked += 1
            except Exception:
                # savepoint 已 rollback（with 區塊離開時自動），只丟這一筆。
                failed += 1
                logger.exception(
                    "離職撤帳失敗（隔離本筆，續處理其餘）employee_id=%s",
                    getattr(record, "employee_id", None),
                )
    return {"revoked": revoked, "failed": failed}


async def run_offboarding_revoke_scheduler(stop_event: asyncio.Event) -> None:
    """每 check_interval 秒掃一次到期離職並補撤帳。"""
    from config import get_settings

    check_interval = get_settings().scheduler.offboarding_revoke_check_interval
    logger.info("offboarding revoke scheduler 啟動 (interval=%ss)", check_interval)
    while not stop_event.is_set():
        # SEC-007：try/except 必須在 scheduler_iteration *外*。scheduler_iteration 本身
        # 已 catch 例外、記 consecutive_failures + heartbeat success=False + 達閾值告警
        # 後才 swallow 保住 loop。若把 try/except 放在 with 內，例外會在到達
        # scheduler_iteration 前被吞掉，使失敗被靜默記成成功（撤權零執行而監控全綠）。
        try:
            with scheduler_iteration(
                "offboarding_revoke", expected_interval_seconds=check_interval
            ):
                result = run_offboarding_revoke_due_once()
                record_rows("offboarding_revoke", int(result.get("revoked", 0)))
        except Exception:
            logger.exception("offboarding revoke scheduler tick 失敗")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
