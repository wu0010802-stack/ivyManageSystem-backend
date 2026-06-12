"""PII Retention GC：定期清除已超過 retention 期的家長 PII。

驅動：個資法第 11 條「特定目的消失應主動刪除」。

- 對象：Guardian 表中 student 已進終態且 terminal_entered_at < NOW - 365 天
- 動作：抹 phone/email/relation/custody_note，name 改 '[已離校家長]'，user_id 解綁
- 不刪 Guardian row、不動 Student PII、不刪 User row
- ENV：PII_RETENTION_GC_DISABLED=1（關閉）/ PII_RETENTION_GC_DRY_RUN=1（只 log）
       / PII_RETENTION_TERMINAL_DAYS=365（可調）

設計選擇：開新檔不擴 security_gc_scheduler（PII GC 是日級且邏輯複雜）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from utils.taipei_time import now_taipei_naive

from sqlalchemy import bindparam, text

from config import get_settings
from models.audit import AuditLog
from models.base import get_session
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)

_GC_INTERVAL_SEC = 24 * 60 * 60
_INITIAL_DELAY_SEC = 60
_BATCH_LIMIT = 500


def scheduler_enabled() -> bool:
    return not bool(get_settings().scheduler.pii_retention_gc_disabled)


def dry_run_enabled() -> bool:
    return bool(get_settings().scheduler.pii_retention_gc_dry_run)


def retention_days() -> int:
    return int(get_settings().scheduler.pii_retention_terminal_days or 365)


def employee_retention_years() -> int:
    return int(get_settings().scheduler.employee_pii_retention_years or 5)


async def run_pii_retention_scheduler(stop_event: asyncio.Event) -> None:
    """主迴圈：每 24 小時跑一次 PII retention GC。"""
    logger.info(
        "pii_retention_scheduler started (dry_run=%s, days=%s)",
        dry_run_enabled(),
        retention_days(),
    )
    try:
        # 啟動後 60 秒首跑（避免冷啟動同時打 DB）
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INITIAL_DELAY_SEC)
            return
        except asyncio.TimeoutError:
            pass

        while not stop_event.is_set():
            with scheduler_iteration(
                "pii_retention", expected_interval_seconds=_GC_INTERVAL_SEC
            ):
                _run_pii_retention_gc()
            with scheduler_iteration(
                "pii_retention_employee", expected_interval_seconds=_GC_INTERVAL_SEC
            ):
                _run_employee_pii_retention_gc()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_GC_INTERVAL_SEC)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("pii_retention_scheduler stopped")


def _run_pii_retention_gc(session=None) -> None:
    """單次 GC：找到期 Guardian → 抹 PII → 寫 audit_log。

    session 參數：None 時內部走 get_session() 取新 session；測試傳入 fixture
    session 以共享 transaction 看到測試先 commit 的 row。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days())
    dry = dry_run_enabled()
    owns_session = session is None
    if owns_session:
        session = get_session()
    try:
        dialect = session.bind.dialect.name
        lock_clause = "FOR UPDATE SKIP LOCKED" if dialect == "postgresql" else ""
        rows = session.execute(
            text(f"""
            SELECT g.id, g.student_id, s.lifecycle_status, s.terminal_entered_at
            FROM guardians g
            JOIN students s ON s.id = g.student_id
            WHERE s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND s.terminal_entered_at IS NOT NULL
              AND s.terminal_entered_at < :cutoff
              AND g.pii_redacted_at IS NULL
              AND g.deleted_at IS NULL
            ORDER BY g.id
            LIMIT :limit
            {lock_clause}
        """),
            {"cutoff": cutoff, "limit": _BATCH_LIMIT},
        ).fetchall()

        if not rows:
            logger.info("pii_retention GC: 無到期 Guardian")
            return

        guardian_ids = [r[0] for r in rows]
        logger.info(
            "pii_retention GC: %s 筆%s",
            len(guardian_ids),
            " (dry-run)" if dry else "",
        )
        for r in rows:
            logger.info(
                "  - guardian_id=%s student_id=%s lifecycle=%s terminal_at=%s",
                r[0],
                r[1],
                r[2],
                r[3],
            )

        if dry:
            if owns_session:
                session.rollback()
            return

        # 抹 PII（單一 UPDATE atomic）
        now = datetime.now(timezone.utc)
        stmt = text("""
            UPDATE guardians
            SET name = '[已離校家長]',
                phone = NULL,
                email = NULL,
                relation = NULL,
                custody_note = NULL,
                user_id = NULL,
                pii_redacted_at = :now,
                updated_at = :now
            WHERE id IN :ids
        """).bindparams(bindparam("ids", expanding=True))
        session.execute(stmt, {"ids": tuple(guardian_ids), "now": now})

        # 同步抹除 students 表上的去正規化家長快照（parent_name / parent_phone 是
        # is_primary guardian 的雙寫副本，見 api/students._sync_*）。只抹 guardians
        # 會讓同一份家長 PII 以明文續存於 students，等同 GC 被繞過（個資法 §11）。
        student_ids = sorted({r[1] for r in rows if r[1] is not None})
        if student_ids:
            student_stmt = text("""
                UPDATE students
                SET parent_name = '[已離校家長]',
                    parent_phone = NULL
                WHERE id IN :sids
            """).bindparams(bindparam("sids", expanding=True))
            session.execute(student_stmt, {"sids": tuple(student_ids)})

        # 寫 audit_log（每筆一條，changes 不含 PII）
        days = retention_days()
        for r in rows:
            session.add(
                AuditLog(
                    user_id=None,
                    username="pii_retention_gc",
                    action="UPDATE",
                    entity_type="guardian",
                    entity_id=str(r[0]),
                    summary=f"PII retention redact (>{days}d after terminal)",
                    changes=json.dumps(
                        {
                            "reason": f"retention_{days}d",
                            "student_id": r[1],
                            "lifecycle_status": r[2],
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=None,
                    created_at=now_taipei_naive(),
                )
            )

        if owns_session:
            session.commit()
        else:
            session.flush()
        logger.info("pii_retention GC: 已抹 %s 筆 Guardian PII", len(guardian_ids))
        record_rows("pii_retention", len(guardian_ids))
    except Exception as e:
        # Downgraded：scheduler 端 wrapper 會做 throttled Sentry 上報
        logger.warning("pii_retention GC 失敗: %s", e, exc_info=True)
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def _run_employee_pii_retention_gc(session=None) -> None:
    """單次 GC：找離職滿 5 年 Employee → 抹通訊 PII → 寫 audit_log。

    抹欄位：address, emergency_contact_name, emergency_contact_phone, bank_account, bank_account_name
    保留欄位：身分證、薪資歷史 (供稅務 query)
    觸發條件：is_active=False AND resign_date < NOW - 5y AND pii_redacted_at IS NULL

    驅動：個資法 §11 特定目的消失應主動刪除；保留期限 5 年參酌商業會計法 +
    勞基法工資紀錄保存 5 年規範。
    """
    cutoff_years = employee_retention_years()
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=cutoff_years * 365)
    dry = dry_run_enabled()
    owns_session = session is None
    if owns_session:
        session = get_session()
    try:
        dialect = session.bind.dialect.name
        lock_clause = "FOR UPDATE SKIP LOCKED" if dialect == "postgresql" else ""
        rows = session.execute(
            text(f"""
            SELECT id, name, resign_date
            FROM employees
            WHERE is_active = FALSE
              AND resign_date IS NOT NULL
              AND resign_date < :cutoff
              AND pii_redacted_at IS NULL
            ORDER BY id
            LIMIT :limit
            {lock_clause}
        """),
            {"cutoff": cutoff, "limit": _BATCH_LIMIT},
        ).fetchall()

        if not rows:
            logger.info("employee_pii_retention GC: 無到期 Employee")
            return

        emp_ids = [r[0] for r in rows]
        logger.info(
            "employee_pii_retention GC: %s 筆%s",
            len(emp_ids),
            " (dry-run)" if dry else "",
        )

        if dry:
            if owns_session:
                session.rollback()
            return

        now = datetime.now(timezone.utc)
        stmt = text("""
            UPDATE employees
            SET address = NULL,
                emergency_contact_name = NULL,
                emergency_contact_phone = NULL,
                bank_account = NULL,
                bank_account_name = NULL,
                pii_redacted_at = :now,
                updated_at = :now
            WHERE id IN :ids
        """).bindparams(bindparam("ids", expanding=True))
        session.execute(stmt, {"ids": tuple(emp_ids), "now": now})

        for r in rows:
            session.add(
                AuditLog(
                    user_id=None,
                    username="pii_retention_gc",
                    action="UPDATE",
                    entity_type="employee",
                    entity_id=str(r[0]),
                    summary=f"Employee PII retention redact (>{cutoff_years}y after resign)",
                    changes=json.dumps(
                        {
                            "reason": f"retention_{cutoff_years}y",
                            "resign_date": str(r[2]),
                            "fields_redacted": [
                                "address",
                                "emergency_contact_name",
                                "emergency_contact_phone",
                                "bank_account",
                                "bank_account_name",
                            ],
                            "fields_preserved": ["id_number", "salary_history"],
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=None,
                    created_at=now_taipei_naive(),
                )
            )

        if owns_session:
            session.commit()
        else:
            session.flush()
        logger.info("employee_pii_retention GC: 已抹 %s 筆 Employee PII", len(emp_ids))
        record_rows("pii_retention_employee", len(emp_ids))
    except Exception as e:
        logger.warning("employee_pii_retention GC 失敗: %s", e, exc_info=True)
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
