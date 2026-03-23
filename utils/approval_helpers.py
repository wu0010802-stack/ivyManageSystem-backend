"""
共用的審核 helper 函式，供 leaves、overtimes、punch_corrections 路由使用。
"""

from models.database import User, ApprovalPolicy, ApprovalLog


def _get_submitter_role(employee_id: int, session) -> str:
    """查詢員工對應 User 帳號的角色，找不到預設 teacher"""
    user = session.query(User).filter(
        User.employee_id == employee_id,
        User.is_active == True,
    ).first()
    return user.role if user else "teacher"


def _check_approval_eligibility(doc_type: str, submitter_role: str, approver_role: str, session) -> bool:
    """查詢 ApprovalPolicy，確認 approver_role 是否有資格審核 submitter_role 的申請"""
    policy = session.query(ApprovalPolicy).filter(
        ApprovalPolicy.is_active == True,
        ApprovalPolicy.submitter_role == submitter_role,
        ApprovalPolicy.doc_type.in_([doc_type, "all"]),
    ).first()
    if not policy:
        return approver_role == "admin"
    return approver_role in [r.strip() for r in policy.approver_roles.split(",")]


def _write_approval_log(doc_type: str, doc_id: int, action: str, approver: dict, comment: str | None, session):
    """寫入簽核記錄"""
    session.add(ApprovalLog(
        doc_type=doc_type,
        doc_id=doc_id,
        action=action,
        approver_id=approver.get("id"),
        approver_username=approver.get("username", ""),
        approver_role=approver.get("role", ""),
        comment=comment,
    ))
