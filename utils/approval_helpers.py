"""
共用的審核 helper 函式，供 leaves、overtimes、punch_corrections 路由使用。
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
