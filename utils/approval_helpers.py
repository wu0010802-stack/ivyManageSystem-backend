"""共用的審核 helper 函式，供 leaves、overtimes、punch_corrections 路由使用。

設計：「任一資格通過即結案」(OR 邏輯)
========================================

審核政策（ApprovalPolicy）的語意是「approver_roles CSV 內任一角色可核准
即放行」，**非**多層 sequential ladder。例如政策
``submitter_role=teacher, approver_roles=supervisor,hr,admin`` 表示
supervisor / hr / admin 三者**任一人**核准就結案，不必依序經過。

Refs: 邏輯漏洞 audit 2026-05-07 P0 #11 — audit reviewer 將「單關通過」
標記為跳過 ladder，但業主於 2026-05-07 確認業務模型即為 OR 邏輯（非
sequential），降為 P1 文件化即可，不動 schema / 邏輯。

額外保留：admin 兜底
- 政策未設定時，approver_role=admin 仍可通過（line 40-47），保證系統
  在新 doc_type 上線初期不會死鎖；其餘角色未設政策一律拒絕。

如果未來業主需要 sequential ladder（例：teacher 送 → supervisor 必先核 →
admin 才能最終核），需要：
1. ApprovalPolicy 加 stage / level 欄位
2. ApprovalLog 累積判斷（看當前 stage 是否到位）
3. 改 leaves.py / overtimes.py / punch_corrections.py 三條 approve 端點
   的 finalize 條件（從「寫一筆 log 即結案」改成「最後一關才結案」）
"""

import json
import logging
from datetime import date

from fastapi import HTTPException

from models.database import User, ApprovalPolicy, ApprovalLog, SalaryRecord

logger = logging.getLogger(__name__)


def _get_submitter_role(employee_id: int, session) -> str:
    """查詢員工對應 User 帳號的角色，找不到預設 teacher"""
    user = (
        session.query(User)
        .filter(
            User.employee_id == employee_id,
            User.is_active == True,
        )
        .first()
    )
    return user.role if user else "teacher"


def _check_approval_eligibility(
    doc_type: str, submitter_role: str, approver_role: str, session
) -> bool:
    """查詢 ApprovalPolicy，確認 approver_role 是否有資格審核 submitter_role 的申請"""
    policy = (
        session.query(ApprovalPolicy)
        .filter(
            ApprovalPolicy.is_active == True,
            ApprovalPolicy.submitter_role == submitter_role,
            ApprovalPolicy.doc_type.in_([doc_type, "all"]),
        )
        .first()
    )
    if not policy:
        # 政策未設定時，允許 admin 作為最後兜底，但記錄 warning 以利追蹤
        if approver_role == "admin":
            logger.warning(
                "審核政策未設定（doc_type=%s, submitter_role=%s），以 admin 身份兜底通過：approver=%s",
                doc_type,
                submitter_role,
                approver_role,
            )
            return True
        logger.warning(
            "審核政策未設定（doc_type=%s, submitter_role=%s），拒絕非 admin 審核：approver_role=%s",
            doc_type,
            submitter_role,
            approver_role,
        )
        return False
    return approver_role in [
        r.strip() for r in (policy.approver_roles or "").split(",")
    ]


def _write_approval_log(
    *,
    session,
    doc_type: str,
    doc_id: int,
    action: str,
    approver: dict,
    comment: str | None = None,
    metadata: dict | None = None,
):
    """寫入簽核記錄並回傳 row（含 id）。日誌寫入失敗時記錄 warning，不阻礙核准主流程。

    Why keyword-only: 三個 router（leaves/overtimes/punch_corrections）共用此 helper，
        metadata 為新增欄位，強制 keyword 呼叫避免位置混淆。
    Why metadata-in-comment: 不動 ApprovalLog schema，用 `[META]` 分隔符嵌入 comment 尾段；
        前端僅顯示 `[META]` 前段，metadata 留給 audit/report 解析。
    Why return row: AuditLog 需在 changes 留下 approval_log_id，方便前端「請假/加班頁的
        簽核紀錄」與「操作紀錄頁」雙向跳轉，不必各自重新撈一次 ApprovalLog。
    """
    try:
        full_comment = comment or ""
        if metadata:
            payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            sep = "\n" if full_comment else ""
            full_comment = f"{full_comment}{sep}[META]{payload}"
        log = ApprovalLog(
            doc_type=doc_type,
            doc_id=doc_id,
            action=action,
            approver_id=approver.get("id"),
            approver_username=approver.get("username", ""),
            approver_role=approver.get("role", ""),
            comment=full_comment or None,
        )
        session.add(log)
        session.flush()  # flush 才會分配 id；同 transaction 內，呼叫端 commit 一次即可
        return log
    except Exception as exc:
        logger.warning(
            "審核日誌寫入失敗（%s #%d action=%s operator=%s）：%s",
            doc_type,
            doc_id,
            action,
            approver.get("username", "unknown"),
            exc,
        )
        return None


def _get_finalized_salary_record(session, employee_id: int, year: int, month: int):
    """查詢單一月份是否已封存。

    找到封存記錄時回傳該 SalaryRecord，否則回傳 None。
    供 meetings / shifts / attendance.records / portal.overtimes 等仍需單月查詢的端點使用；
    leaves / overtimes / punch_corrections 已改用 services.salary.finalize_guard.assert_months_not_finalized。
    """
    return (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
            SalaryRecord.is_finalized == True,
        )
        .first()
    )


# ──────────────────────────────────────────────────────────────────────
# leaves / overtimes / punch_corrections 共用守衛 helpers
# ──────────────────────────────────────────────────────────────────────
# Why: 單側加 guard 漏同步另一側的 P1 bug 多次出現
# （memory project_leaves_overtimes_bug_batch_2026_05_12 P1-5 即此 pattern）。
# 集中於本檔，新增規則只改一處，三條 router 同步。


def is_self_approval(approver: dict, owner_employee_id: int) -> bool:
    """申請人與核准人是否為同一員工。

    純管理員（無 employee_id）本身不會提出申請，不構成自我核准風險，回傳 False。
    用於 leaves / overtimes / punch_corrections 單筆與批次核准守衛。
    """
    approver_eid = approver.get("employee_id")
    return bool(approver_eid and owner_employee_id == approver_eid)


def assert_approver_eligible(
    session,
    *,
    doc_type: str,
    doc_label: str,
    submitter_employee_id: int,
    approver_role: str,
) -> str:
    """整合「查申請人角色 → 檢查 approver 資格 → 不符則 403」三步驟。

    用於 leaves / overtimes / punch_corrections 單筆核准端點。
    批次端點因需 cache submitter_role → eligibility 重複查詢，仍呼叫
    _get_submitter_role + _check_approval_eligibility 低階 helper。

    回傳 submitter_role 供 caller 後續記錄稽核欄位使用。
    """
    submitter_role = _get_submitter_role(submitter_employee_id, session)
    if not _check_approval_eligibility(
        doc_type, submitter_role, approver_role, session
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）"
                f"的{doc_label}申請"
            ),
        )
    return submitter_role


def collect_months_from_date_range(start: date, end: date) -> set[tuple[int, int]]:
    """蒐集 [start, end] 區間涵蓋的所有 (year, month) tuple。

    跨月假單 / 跨日加班會橫跨多個薪資月份，呼叫端需取得完整集合做：
    - assert_months_not_finalized（封存守衛）
    - lock_and_premark_stale（薪資鎖 + needs_recalc 預標）

    Why: api/leaves.py L1261 原為 inline while 迴圈 12 行；overtimes 用
    services.salary.utils.collect_months_from_dates 單日輸入。本 helper
    統一跨日場景，single-date 場景仍可呼叫（start=end）。
    """
    months: set[tuple[int, int]] = set()
    cur = date(start.year, start.month, 1)
    end_first = date(end.year, end.month, 1)
    while cur <= end_first:
        months.add((cur.year, cur.month))
        cur = (
            date(cur.year + 1, 1, 1)
            if cur.month == 12
            else date(cur.year, cur.month + 1, 1)
        )
    return months
