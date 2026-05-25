"""High-risk audit event 過濾與分類。

「高風險」定義（與 spec §3.2 對齊）：
- HTTP DELETE：action='DELETE'
- BLOCKED_*：身份驗證 / 權限伺服器擋下的 403 嘗試
- 非 HTTP DELETE 但 summary 含「(不可復原)」（§2 mark_hard_delete 標的）
- 權限變更：entity_type='user' AND action='UPDATE' AND summary 含 role/permission/角色/權限

False positive 緩解：權限變更 LIKE 只在 entity_type='user' AND action='UPDATE'
組合下生效；若實際 false positive 多再加 marker。
"""

from datetime import datetime
from typing import Literal

import sqlalchemy as sa

from models.audit import AuditLog

HIGH_RISK_ACTIONS = {
    "DELETE",
    "BLOCKED_CREATE",
    "BLOCKED_UPDATE",
    "BLOCKED_DELETE",
}


def filter_high_risk(query, *, since: datetime, only_unack: bool = True):
    """套用 high-risk filter 到 SQLAlchemy query。

    Args:
        query: sa.select(AuditLog) base query
        since: 只看 created_at >= since 的 row
        only_unack: 預設 True，只回未 ack 的 row

    Returns:
        加上 filter / order 的 query（caller 自行 execute）
    """
    cond = sa.or_(
        AuditLog.action.in_(HIGH_RISK_ACTIONS),
        AuditLog.summary.like("%(不可復原)%"),
        sa.and_(
            AuditLog.entity_type == "user",
            AuditLog.action == "UPDATE",
            sa.or_(
                AuditLog.summary.like("%role%"),
                AuditLog.summary.like("%permission%"),
                AuditLog.summary.like("%角色%"),
                AuditLog.summary.like("%權限%"),
            ),
        ),
    )
    query = query.filter(AuditLog.created_at >= since).filter(cond)
    if only_unack:
        query = query.filter(AuditLog.acknowledged_at.is_(None))
    return query.order_by(AuditLog.created_at.desc())


def classify_risk_kind(
    row: AuditLog,
) -> Literal["hard_delete", "blocked", "permission_change"]:
    """分類單筆 row 的 risk kind（response shape 用）。"""
    if row.action in {"BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE"}:
        return "blocked"
    if row.action == "DELETE":
        return "hard_delete"
    if row.summary and "(不可復原)" in row.summary:
        return "hard_delete"
    return "permission_change"
