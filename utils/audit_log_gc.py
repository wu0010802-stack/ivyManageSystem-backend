"""utils/audit_log_gc.py — audit_logs 保留期 GC。

P0b 法規/個資 sprint：個資法 §11（特定目的消失應主動刪除）+ GDPR Art. 5(1)(e)。

Retention policy by entity_type:
- 金流稅務 7 年 (稅捐稽徵法 §30): salary / fee / overtime / vendor_payment /
  year_end_cycle / year_end_settlement / year_end_special_bonus / appraisal_payout /
  appraisal_summary / appraisal_bonus_rate / monthly_fixed_cost /
  disciplinary_action / art_teacher_payroll
- 認證 6 個月 (個資法 §11 必要範圍): auth
- 學生/員工資料 3 年 (個資法 §11 + 兒少): student / employee / guardian /
  parent / classroom / enrollment / recruitment / appraisal / attendance /
  leave / medical
- Fallback 3 年: 其他全部 entity_type（保守 default）

設計：
- 跑頻率 daily（heartbeat 60s 內檢查 last_run > 24h）
- advisory_lock 防多 worker 並發
- 分組 batched DELETE（per-entity_type batch 10000）避免長交易
- record_rows 寫 scheduler observability

Refs: spec docs/superpowers/specs/2026-05-28-audit-pii-redact-retention-design.md
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import text

from utils.advisory_lock import try_scheduler_lock
from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10000

# Retention table (天數)
_FINANCE_DAYS = 365 * 7
_AUTH_DAYS = 30 * 6
_STUDENT_DAYS = 365 * 3
_FALLBACK_DAYS = 365 * 3

# Entity type 分組
# 設計審查 2026-06-25 Finding D：原清單塞了從不被 AuditMiddleware 發出的死值
# （year_end / salary_record / payslip / bonus / fee_record / appraisal_year_end），
# 卻漏了 utils/audit.py ENTITY_PATTERNS 實際會發出的金流 entity_type → 那些金流
# 軌跡落 3 年 fallback 而非稅捐稽徵法 §30 要求的 7 年。修正：移除死值，補上實際
# 會發出且牽動薪資/獎金/付款（金流）的 entity_type。
# 註：salary_record / fee_record / bonus 等字串雖在 codebase 別處出現，但都不是
# AuditLog.entity_type（data_quality Violation / seedgen 假資料 / config 快取 key），
# 對 audit_logs GC 而言為死值。
_FINANCE_TYPES: frozenset[str] = frozenset(
    {
        "salary",
        "fee",
        "overtime",
        "vendor_payment",
        # 年終獎金結算（E 化獨立轉帳，金流）
        "year_end_cycle",
        "year_end_settlement",
        "year_end_special_bonus",
        "appraisal_payout",
        # 考核結算 / 獎金率（影響獎金金額）
        "appraisal_summary",
        "appraisal_bonus_rate",
        # 月度固定費用（金流）
        "monthly_fixed_cost",
        # 員工懲處扣薪 / 才藝鐘點費（皆牽動薪資）
        "disciplinary_action",
        "art_teacher_payroll",
    }
)
_AUTH_TYPES: frozenset[str] = frozenset({"auth"})
_STUDENT_TYPES: frozenset[str] = frozenset(
    {
        "student",
        "employee",
        "guardian",
        "parent",
        "classroom",
        "enrollment",
        "recruitment",
        "appraisal",
        "attendance",
        "leave",
        "medical",
    }
)


def _retention_days_for(entity_type: str) -> int:
    """依 entity_type 回對應 retention 天數。未知 type 走 fallback。"""
    if entity_type in _FINANCE_TYPES:
        return _FINANCE_DAYS
    if entity_type in _AUTH_TYPES:
        return _AUTH_DAYS
    if entity_type in _STUDENT_TYPES:
        return _STUDENT_DAYS
    return _FALLBACK_DAYS


def cleanup_audit_logs(session) -> int:
    """掃 audit_logs，按 entity_type retention 分批刪過期列。

    回傳：總刪除筆數。
    分批：每組 entity_type 內每次最多 _BATCH_SIZE 列，loop until done。
    """
    now = now_taipei_naive()
    total_deleted = 0

    # Finding F：audit_logs immutable trigger（auditrelax01）只放行 audit_archiver role
    # 執行 DELETE；GC 走一般 admin 連線會被 trigger 擋 → 個資法 §11 retention 靜默失效
    # + 表無上限成長。PG 上於每個 batch 交易內 SET LOCAL ROLE audit_archiver（對齊
    # _write_audit_sync 的 ivy_audit_writer 模式；SET LOCAL 隨交易結束自動還原）。
    # prod 須先 CREATE ROLE audit_archiver 並 GRANT 給連線登入角色，否則 SET ROLE 失敗
    # → 此處一次性 probe 後 raise（P2：不再 fail-soft 靜默，讓外層 scheduler_iteration
    # 計入失敗並上報 Sentry/LINE，避免 retention 永不執行卻監控全綠）。
    is_pg = False
    try:
        is_pg = session.get_bind().dialect.name == "postgresql"
    except Exception:
        is_pg = False
    if is_pg:
        try:
            session.execute(text("SET LOCAL ROLE audit_archiver"))
            session.rollback()
        except Exception as e:
            session.rollback()
            # P2：不可靜默 return 0。SET ROLE 失敗代表 prod 漏建 audit_archiver role →
            # audit_logs §11 retention 將永不執行、表無上限成長。若 return 0，外層
            # scheduler_iteration 視為成功（consecutive_failures=0）→ capture_exception
            # / LINE 告警都不觸發 → GC 靜默失效卻監控全綠。改 raise，讓 scheduler_iteration
            # 計入 consecutive_failures，達 ALERT_THRESHOLD 後上報 Sentry + LINE 告警。
            logger.error(
                "audit_log GC 中止：SET ROLE audit_archiver 失敗（prod 須先 CREATE "
                "ROLE audit_archiver 並 GRANT 給連線登入角色，見 auditrelax01 migration "
                "說明）：%s",
                e,
            )
            raise RuntimeError(
                "audit_log GC 無法執行：SET ROLE audit_archiver 失敗（prod 須先 CREATE "
                "ROLE audit_archiver 並 GRANT 給連線登入角色）"
            ) from e

    # 先取得 DB 中現有的 entity_type 清單（避免硬編一份 list 跟現實不同步）
    rows = session.execute(
        text(
            "SELECT DISTINCT entity_type FROM audit_logs WHERE entity_type IS NOT NULL"
        )
    ).all()
    entity_types = [r[0] for r in rows]

    for et in entity_types:
        cutoff = now - timedelta(days=_retention_days_for(et))
        et_deleted = 0
        # 多輪 batch 直到該 entity_type 的過期列清完
        while True:
            # PG：以 audit_archiver role 執行 DELETE（immutable trigger 僅放行此 role）。
            # SET LOCAL 與下方 DELETE/commit 同一交易，交易結束自動還原為原連線角色。
            if is_pg:
                session.execute(text("SET LOCAL ROLE audit_archiver"))
            # PG / SQLite 都支援 DELETE ... WHERE id IN (SELECT id ... LIMIT)
            result = session.execute(
                text("""
                    DELETE FROM audit_logs
                    WHERE id IN (
                        SELECT id FROM audit_logs
                        WHERE created_at < :cutoff
                          AND entity_type = :et
                        LIMIT :batch
                    )
                    """),
                {"cutoff": cutoff, "et": et, "batch": _BATCH_SIZE},
            )
            batch_deleted = result.rowcount or 0
            session.commit()
            et_deleted += batch_deleted
            if batch_deleted < _BATCH_SIZE:
                break

        if et_deleted > 0:
            logger.info(
                "audit_log GC: entity_type=%s retention_days=%d deleted=%d",
                et,
                _retention_days_for(et),
                et_deleted,
            )
        total_deleted += et_deleted

    return total_deleted


def run_audit_log_gc_once(
    session_factory, expected_interval_seconds: int | None = None
) -> int:
    """跑一次 audit_log GC。caller 端負責 advisory_lock 與 disabled flag 判斷。

    `session_factory`: zero-arg callable 回傳 SQLAlchemy session（contextmanager）。
    `expected_interval_seconds`: 選填。傳入則 scheduler_iteration 會 UPSERT
        scheduler_heartbeats row，讓 /health/schedulers 與外部 watchdog 看得到這支
        法遵 GC 的最近成功時間（未傳則 process restart 後對它全盲）。caller
        （services/security_gc_scheduler._run_audit_log_gc）傳 _AUDIT_LOG_GC_INTERVAL_SEC。
    """
    from models.base import session_scope

    # 回傳必為 int：cleanup 內 raise（prod 漏建 audit_archiver role）會被
    # scheduler_iteration by-design 吞掉，故 `return deleted` 不可寫在該 with
    # 區塊內——否則例外被吞後 fall-through 回 None，caller 端 `if deleted > 0`
    # 會炸 TypeError 並用誤導的 ERROR 蓋掉真正原因。預設 0，在區塊外 return。
    deleted = 0
    with scheduler_iteration(
        "security_audit_log_gc", expected_interval_seconds=expected_interval_seconds
    ):
        with session_scope() as lock_session:
            with try_scheduler_lock(
                lock_session,
                scheduler_name="security_audit_log_gc",
                run_key=str(int(time.time() // 86400)),  # 日 bucket
            ) as acquired:
                if acquired:
                    with session_scope() as work_session:
                        deleted = cleanup_audit_logs(work_session)
                        record_rows("security_audit_log_gc", int(deleted))
    return deleted
