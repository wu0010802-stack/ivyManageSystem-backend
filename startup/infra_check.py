"""啟動期 DB 基礎建設 schema-drift 偵測（read-only；設計審查 2026-06-25 主題 B）。

prod 曾以 ``create_all + alembic stamp head`` 建立，**跳過所有 migration 的 op.execute
基礎建設**（DB role / SECURITY DEFINER function / immutability trigger / RLS policy +
FORCE / partial unique index），但 ``alembic_version`` 卻被 stamp 成 heads → 系統
「以為」fully migrated 卻無這些保護。涵蓋：
  - 稽核 / 給藥紀錄不可竄改（audit_log / medication_log immutable trigger + function）
  - 家長端 RLS 隔離（ivy_parent_role + parent_owns_attachment + parent_isolate_* policy
    + FORCE ROW LEVEL SECURITY；少了 role/function 或表沒 FORCE，家長可越界讀他人資料）
  - 金流 / 醫療 / 單例 資料完整性的 partial unique index（並發去重最後防線）

本模組在啟動 **唯讀偵測** 關鍵物件是否存在；缺漏 → logger.warning + Sentry
capture_message，讓 fresh/DR/新環境的 divergence 在 prod 可見而非靜默失去保護。

設計（避免脆弱列舉）：家長 RLS policy 在 migration 以 f-string 迴圈產生（無法純文字
列舉 33 個名），故不逐一列舉 policy 名；改以「① ivy_parent_role + parent_owns_attachment
等精確物件（fresh DB 漏 RLS 必然漏這些）② 結構性檢查：任何掛了 parent_% policy 的表
卻未 FORCE RLS（owner 可繞過）」偵測，零列舉。functions / triggers / roles / 關鍵
partial index 用精確名；漂移由 tests/test_db_infra_check.py 掃 migration 反推護線。

注意：本模組**只偵測不修補**——重建這些 PG-專屬 DDL（idempotent ensure_*）屬另一個
需在 staging 實 DB 驗證的增量（SQLite 測試環境無法 TDD PG RLS/role/function）。非 PG
（SQLite 測試/dev）→ 回 [] 不偵測、不阻擋啟動。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 自 alembic/versions op.execute 抽出的關鍵物件（2026-06-25 盤點 + subagent 完整性核實）。
# 新增 SECURITY DEFINER function / immutability trigger / DB role 的 migration 時須同步
# 登記；tests/test_db_infra_check.py 掃 migration 反推，漏登即 CI 紅（防漂移）。
CRITICAL_DB_FUNCTIONS = (
    "audit_log_immutable_fn",
    "medication_log_immutable_fn",
    "parent_owns_attachment",  # 家長 RLS policy 的 USING 子句依賴此 function
    "public_count_enrolled",
)
CRITICAL_DB_TRIGGERS = (
    "trg_audit_log_immutable_delete",
    "trg_audit_log_immutable_update",
    "trg_medication_log_immutable",
)
# 家長 RLS / 稽核寫入依賴的 DB role（policy 的 TO ivy_parent_role、GRANT 都依賴 role 存在）。
CRITICAL_DB_ROLES = (
    "ivy_parent_role",
    "ivy_admin_role",
    "ivy_audit_writer",
    "audit_archiver",
)
# 金流 / 醫療 / 單例 資料完整性最後防線（並發去重；create_all 不一定重建 partial WHERE）。
CRITICAL_PARTIAL_UNIQUE_INDEXES = (
    "uq_salary_snapshot_emp_ym_immutable",  # 薪資快照不可重複（金流稽核）
    "uq_salary_calc_jobs_active_ym",  # 同月只一個 active 結算 job
    "ix_fee_records_monthly_unique",  # 月費紀錄唯一（帳務）
    "uq_fee_records_non_monthly_unique",  # 非月費紀錄唯一
    "uq_medication_logs_order_slot_primary",  # 給藥單同時段唯一（醫療）
    "uq_academic_terms_is_current_singleton",  # 當前學期單例
    "uq_students_id_number_notnull",  # 學生身分證唯一（去重）
    "uq_activity_regs_student_term_active",  # 才藝報名並發去重
)


def compute_missing_infra(
    found_funcs,
    found_triggers,
    found_roles,
    found_indexes,
    tables_policy_without_force,
) -> list[str]:
    """純函式：給「DB 實際存在的物件集合」算出缺漏清單（``type:name``），與 I/O 分離易測。

    tables_policy_without_force：掛了 parent_% policy 卻未 FORCE RLS 的表清單
    （owner 可繞過 → 等同 RLS 失效）。
    """
    missing: list[str] = []
    missing += [
        f"function:{n}" for n in CRITICAL_DB_FUNCTIONS if n not in set(found_funcs)
    ]
    missing += [
        f"trigger:{n}" for n in CRITICAL_DB_TRIGGERS if n not in set(found_triggers)
    ]
    missing += [f"role:{n}" for n in CRITICAL_DB_ROLES if n not in set(found_roles)]
    missing += [
        f"partial_index:{n}"
        for n in CRITICAL_PARTIAL_UNIQUE_INDEXES
        if n not in set(found_indexes)
    ]
    missing += [f"rls_not_forced:{t}" for t in sorted(set(tables_policy_without_force))]
    return sorted(missing)


def check_db_infra_present(session) -> list[str]:
    """啟動期偵測關鍵 op.execute 基礎建設是否存在，回傳缺漏清單。

    非 PostgreSQL（SQLite 測試/dev）→ 回 []。查詢失敗 → 回 [] 不阻擋啟動。
    偵測到缺漏 → logger.warning + Sentry capture_message。
    """
    from sqlalchemy import text

    dialect = ""
    try:
        if session.bind is not None:
            dialect = session.bind.dialect.name
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return []

    try:
        found_funcs = (
            session.execute(
                text("SELECT proname FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": list(CRITICAL_DB_FUNCTIONS)},
            )
            .scalars()
            .all()
        )
        found_triggers = (
            session.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname = ANY(:n)"
                ),
                {"n": list(CRITICAL_DB_TRIGGERS)},
            )
            .scalars()
            .all()
        )
        found_roles = (
            session.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:n)"),
                {"n": list(CRITICAL_DB_ROLES)},
            )
            .scalars()
            .all()
        )
        found_indexes = (
            session.execute(
                text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:n)"),
                {"n": list(CRITICAL_PARTIAL_UNIQUE_INDEXES)},
            )
            .scalars()
            .all()
        )
        # 結構性 FORCE 檢查：掛 parent_% policy 卻未 FORCE RLS 的表（owner 繞過 → RLS 失效）。
        # 零列舉——直接問 DB「哪些有家長 policy 的表沒 FORCE」。
        tables_policy_without_force = (
            session.execute(
                text(
                    "SELECT DISTINCT p.tablename FROM pg_policies p "
                    "JOIN pg_class c ON c.relname = p.tablename "
                    "WHERE p.policyname LIKE 'parent\\_%' "
                    "AND NOT c.relforcerowsecurity"
                )
            )
            .scalars()
            .all()
        )
    except Exception as e:  # noqa: BLE001 — 偵測查詢失敗不阻擋啟動
        logger.warning("DB infra check 查詢失敗（不阻擋啟動）: %s", e)
        return []

    missing = compute_missing_infra(
        found_funcs,
        found_triggers,
        found_roles,
        found_indexes,
        tables_policy_without_force,
    )
    if missing:
        msg = (
            f"DB 關鍵基礎建設缺漏 {len(missing)} 項（create_all+stamp 可能跳過 migration "
            f"op.execute；稽核/給藥不可竄改、家長 RLS 隔離、金流/醫療去重可能失效）：{missing}"
        )
        logger.warning(msg)
        try:
            from utils.sentry_init import capture_message

            capture_message(msg, level="warning")
        except Exception:  # noqa: BLE001
            pass
    return missing
