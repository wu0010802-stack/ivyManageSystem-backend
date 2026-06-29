"""PII Retention GC：定期清除已超過 retention 期的家長 PII。

驅動：個資法第 11 條「特定目的消失應主動刪除」。

- 對象：Guardian 表中 student 已進終態且 terminal_entered_at < NOW - 365 天
- 動作：抹 phone/email/relation/custody_note，name 改 '[已離校家長]'，user_id 解綁
- 同步抹去正規化的「家長」PII 副本：students.parent_name/parent_phone、
  activity_registrations.parent_phone/email（否則同一份家長 PII 以明文續存於
  雙寫副本表，等同 GC 被繞過，個資法 §11）
- 不刪 Guardian row、不刪 User row；**不動學生本人 PII**（Student.name/birthday、
  activity_registrations.student_name/birthday 皆保留——只抹「家長」欄位）
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

_PARENT_PII_PLACEHOLDER = "[已離校家長]"

# 終態學生「家長 PII 去正規化副本」位置 — 單一事實來源（個資法 §11；系統設計審查
# 2026-06-25 主題 B）。GC 抹除以此驅動：新增任何雙寫家長 PII 的表只要登記於此，GC
# 自動涵蓋，且 tests/test_pii_retention_gc.py 的 completeness 守衛會掃 model 強制登記
# （防再漏一張表，如 2026-06-25 前 activity_registrations 被漏抹）。
#   - link_column：該表連到「終態學生 id」的欄位（students 為自身 id，副本表為 student_id）
#   - null_columns / placeholder_columns：要抹的家長欄位
# 注意：① 學生本人 PII（student_name/birthday/Student.name）不在此，依 retention 政策
# 保留；② guardians 本身是 GC 驅動查詢（有 pii_redacted_at 冪等戳記），單獨處理不列此。
PARENT_PII_DENORMALIZED_LOCATIONS: list[dict] = [
    {
        "table": "students",
        "link_column": "id",
        "null_columns": ["parent_phone"],
        "placeholder_columns": {"parent_name": _PARENT_PII_PLACEHOLDER},
    },
    {
        "table": "activity_registrations",
        "link_column": "student_id",
        "null_columns": ["parent_phone", "email"],
        "placeholder_columns": {},
    },
]


def _redact_denormalized_location(session, loc: dict, student_ids: list[int]) -> int:
    """依 registry 抹單一去正規化表的家長 PII 副本，回傳受影響 row 數。

    SQL 的表名/欄名全來自 PARENT_PII_DENORMALIZED_LOCATIONS 常數（非使用者輸入），
    無注入風險；`AND (... IS NOT NULL)` 讓已抹的 row 不重複計數（冪等且 rowcount 準確）。
    """
    set_clauses = [f"{c} = NULL" for c in loc["null_columns"]]
    params: dict = {"sids": tuple(student_ids)}
    for col, val in loc["placeholder_columns"].items():
        set_clauses.append(f"{col} = :_ph_{col}")
        params[f"_ph_{col}"] = val
    touched = list(loc["null_columns"]) + list(loc["placeholder_columns"])
    guard = " OR ".join(f"{c} IS NOT NULL" for c in touched)
    sql = (
        f"UPDATE {loc['table']} SET {', '.join(set_clauses)} "
        f"WHERE {loc['link_column']} IN :sids AND ({guard})"
    )
    stmt = text(sql).bindparams(bindparam("sids", expanding=True))
    return session.execute(stmt, params).rowcount


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
                await asyncio.to_thread(_run_pii_retention_gc)
            with scheduler_iteration(
                "pii_retention_employee", expected_interval_seconds=_GC_INTERVAL_SEC
            ):
                await asyncio.to_thread(_run_employee_pii_retention_gc)
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
              -- P2：不可加 g.deleted_at IS NULL。軟刪除的 Guardian（監護權變更/離婚/
              -- 誤建修正）反而最該抹 PII——否則離開系統的家長個資（手機/Email/LINE
              -- user_id/監護說明）永久殘留 guardians 表（個資法 §11 破口）。
              -- pii_redacted_at IS NULL 已確保 idempotency（已抹的不再抹）。
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

        # 在 guardians.user_id 被 NULL 前，先捕捉受影響的家長 User id。抹完 guardian 後若
        # 某 User 已無任何 guardian 指向（孤兒），其 LINE 身分 PII（display_name 常為家長真名、
        # line_user_id 為 LINE 全球唯一識別碼）仍永久殘留 users 表，等同 GC 對該家長部分被繞過
        # （個資法 §11，qa-loop round2 2026-06-29）。
        affected_user_ids = [
            row[0]
            for row in session.execute(
                text(
                    "SELECT DISTINCT user_id FROM guardians "
                    "WHERE id IN :ids AND user_id IS NOT NULL"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": tuple(guardian_ids)},
            ).fetchall()
        ]

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

        # 同步抹除「家長 PII 去正規化副本」（students.parent_*、activity_registrations
        # 的 parent_phone/email…）。只抹 guardians 會讓同一份家長 PII 以明文續存於
        # 副本表，等同 GC 被繞過（個資法 §11）。抹除位置由 PARENT_PII_DENORMALIZED_LOCATIONS
        # registry 單一來源驅動——新增雙寫表只要登記即自動涵蓋（主題 B：副本散落多表）。
        student_ids = sorted({r[1] for r in rows if r[1] is not None})
        denorm_redacted: dict[str, int] = {}
        if student_ids:
            for loc in PARENT_PII_DENORMALIZED_LOCATIONS:
                denorm_redacted[loc["table"]] = _redact_denormalized_location(
                    session, loc, student_ids
                )
        areg_redacted = denorm_redacted.get("activity_registrations", 0)

        # 抹孤兒家長 User 的 LINE 身分 PII。NOT EXISTS 守衛確保只抹「已無任何 guardian 指向」
        # 的 User——仍有在籍/未抹子女的家長不受影響；line_user_id IS NOT NULL 限定只動 LINE
        # 家長帳號（員工帳號無此欄），不誤傷後台 staff。username 改不可回溯值維持 unique 約束。
        orphan_users_redacted = 0
        if affected_user_ids:
            orphan_res = session.execute(
                text("""
                    UPDATE users
                    SET display_name = NULL,
                        line_user_id = NULL,
                        username = 'redacted_parent_' || id
                    WHERE id IN :uids
                      AND line_user_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM guardians g WHERE g.user_id = users.id
                      )
                    """).bindparams(bindparam("uids", expanding=True)),
                {"uids": tuple(affected_user_ids)},
            )
            orphan_users_redacted = orphan_res.rowcount or 0
            if orphan_users_redacted:
                logger.info(
                    "pii_retention GC: 抹除 %d 個孤兒家長 User 的 LINE 身分 PII",
                    orphan_users_redacted,
                )

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
        logger.info(
            "pii_retention GC: 已抹 %s 筆 Guardian PII（含 %s 筆才藝報名去正規化副本）",
            len(guardian_ids),
            areg_redacted,
        )
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
