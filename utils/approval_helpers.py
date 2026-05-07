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

import logging

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
    doc_type: str,
    doc_id: int,
    action: str,
    approver: dict,
    comment: str | None,
    session,
):
    """寫入簽核記錄並回傳 row（含 id）。日誌寫入失敗時記錄 warning，不阻礙核准主流程。

    Why return row: AuditLog 需在 changes 留下 approval_log_id，方便前端「請假/加班頁的
    簽核紀錄」與「操作紀錄頁」雙向跳轉，不必各自重新撈一次 ApprovalLog。
    """
    try:
        log = ApprovalLog(
            doc_type=doc_type,
            doc_id=doc_id,
            action=action,
            approver_id=approver.get("id"),
            approver_username=approver.get("username", ""),
            approver_role=approver.get("role", ""),
            comment=comment,
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
    供 leaves.py（多月份迴圈）與 overtimes.py（單月份）共用。
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
