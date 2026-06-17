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

# 權限變更偵測關鍵字（filter_high_risk 的 SQL LIKE 與 is_high_risk_event 的 Python
# 比對共用同一份，避免 read-time 與 write-time 偵測漂移）。
_PERMISSION_KEYWORDS = ("role", "permission", "角色", "權限")
_HARD_DELETE_MARKER = "(不可復原)"


def is_high_risk_event(
    action: str, summary: str | None, entity_type: str | None
) -> bool:
    """Row 層級高風險判定（write-time 用），與 filter_high_risk 的 SQL 條件等價。

    主動告警掛在稽核寫入後，用此判斷是否值得推播；條件須與 read-time
    filter_high_risk 一致，否則紅點清單與 LINE 告警會對不上。
    """
    if action in HIGH_RISK_ACTIONS:
        return True
    if summary and _HARD_DELETE_MARKER in summary:
        return True
    if (
        entity_type == "user"
        and action == "UPDATE"
        and summary
        and any(kw in summary for kw in _PERMISSION_KEYWORDS)
    ):
        return True
    return False


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
        AuditLog.summary.like(f"%{_HARD_DELETE_MARKER}%"),
        sa.and_(
            AuditLog.entity_type == "user",
            AuditLog.action == "UPDATE",
            sa.or_(*[AuditLog.summary.like(f"%{kw}%") for kw in _PERMISSION_KEYWORDS]),
        ),
    )
    query = query.filter(AuditLog.created_at >= since).filter(cond)
    if only_unack:
        query = query.filter(AuditLog.acknowledged_at.is_(None))
    return query.order_by(AuditLog.created_at.desc())


def classify_risk_kind_fields(
    action: str, summary: str | None
) -> Literal["hard_delete", "blocked", "permission_change"]:
    """以 (action, summary) 欄位分類 risk kind（write-time 用，無需 ORM row）。"""
    if action in {"BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE"}:
        return "blocked"
    if action == "DELETE":
        return "hard_delete"
    if summary and _HARD_DELETE_MARKER in summary:
        return "hard_delete"
    return "permission_change"


def classify_risk_kind(
    row: AuditLog,
) -> Literal["hard_delete", "blocked", "permission_change"]:
    """分類單筆 row 的 risk kind（response shape 用）。"""
    return classify_risk_kind_fields(row.action, row.summary)
