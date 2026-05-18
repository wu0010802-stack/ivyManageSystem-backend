"""考核簽核流程 helper（Phase 2 signing UX）。

純函式設計，不接 DB。實際 endpoint 在 api/appraisal/__init__.py
用這裡的 helper 做 status transition 判斷與 log writing 共用邏輯。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalSummary,
    AppraisalSummaryLog,
    SummaryLogAction,
    SummaryStatus,
)

_ADVANCE_MAP = {
    (SummaryStatus.DRAFT, "SUPERVISOR"): SummaryStatus.SUPERVISOR_SIGNED,
    (SummaryStatus.SUPERVISOR_SIGNED, "ACCOUNTING"): SummaryStatus.ACCOUNTING_SIGNED,
    (SummaryStatus.ACCOUNTING_SIGNED, "FINALIZE"): SummaryStatus.FINALIZED,
}

_VALID_REJECT_TARGETS = {
    SummaryStatus.SUPERVISOR_SIGNED: {SummaryStatus.DRAFT},
    SummaryStatus.ACCOUNTING_SIGNED: {
        SummaryStatus.DRAFT,
        SummaryStatus.SUPERVISOR_SIGNED,
    },
    SummaryStatus.FINALIZED: {SummaryStatus.ACCOUNTING_SIGNED},
}

_DEFAULT_REJECT = {
    SummaryStatus.SUPERVISOR_SIGNED: SummaryStatus.DRAFT,
    SummaryStatus.ACCOUNTING_SIGNED: SummaryStatus.SUPERVISOR_SIGNED,
    SummaryStatus.FINALIZED: SummaryStatus.ACCOUNTING_SIGNED,
}


def can_advance(current: SummaryStatus, stage: str) -> bool:
    """stage in {SUPERVISOR, ACCOUNTING, FINALIZE}"""
    return (current, stage) in _ADVANCE_MAP


def advance_target(current: SummaryStatus, stage: str) -> Optional[SummaryStatus]:
    return _ADVANCE_MAP.get((current, stage))


def can_reject(current: SummaryStatus, to_status: SummaryStatus) -> bool:
    return to_status in _VALID_REJECT_TARGETS.get(current, set())


def default_reject_to_status(current: SummaryStatus) -> Optional[SummaryStatus]:
    return _DEFAULT_REJECT.get(current)


def write_summary_log(
    session: Session,
    summary: AppraisalSummary,
    action: SummaryLogAction,
    actor_user_id: int,
    actor_role: Optional[str] = None,
    from_status: Optional[SummaryStatus] = None,
    to_status: Optional[SummaryStatus] = None,
    reason: Optional[str] = None,
    comment: Optional[str] = None,
) -> AppraisalSummaryLog:
    """INSERT 一條 log；caller 負責 commit / flush。"""
    log = AppraisalSummaryLog(
        summary_id=summary.id,
        action=action,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_user_id,
        actor_role_snapshot=actor_role,
        reason=reason,
        comment=comment,
    )
    session.add(log)
    return log


def clear_rejection_state(summary: AppraisalSummary) -> None:
    """簽核成功進階時清掉 rejected_* 殘影（P1-5）。

    威脅：reject 後 summary 留下 rejected_at / rejected_by / rejected_from_stage /
    rejected_reason 四個欄位，若之後再從同一階段往上簽，欄位殘留會讓 UI
    把已成功進階的 summary 顯示為「曾被退簽」狀態，造成混淆。caller 在
    sign_supervisor / sign_accounting / finalize 成功時呼叫此 helper 即可。
    """
    summary.rejected_at = None
    summary.rejected_by = None
    summary.rejected_from_stage = None
    summary.rejected_reason = None


def apply_reject(
    summary: AppraisalSummary,
    to_status: SummaryStatus,
    actor_user_id: int,
    reason: str,
) -> SummaryStatus:
    """變更 summary status 並清除超過 to_status 階的 timestamp/by/comment。

    回傳：from_status（給 caller 寫 log 用）
    """
    from_status = summary.status
    summary.status = to_status
    summary.rejected_at = datetime.now(timezone.utc)
    summary.rejected_by = actor_user_id
    summary.rejected_from_stage = from_status
    summary.rejected_reason = reason

    # 清掉超過 to_status 階的 sign 欄位
    if to_status in (SummaryStatus.DRAFT,):
        summary.supervisor_signed_at = None
        summary.supervisor_signed_by = None
        summary.supervisor_comment = None
    if to_status in (SummaryStatus.DRAFT, SummaryStatus.SUPERVISOR_SIGNED):
        summary.accounting_signed_at = None
        summary.accounting_signed_by = None
        summary.accounting_comment = None
    if to_status in (
        SummaryStatus.DRAFT,
        SummaryStatus.SUPERVISOR_SIGNED,
        SummaryStatus.ACCOUNTING_SIGNED,
    ):
        summary.finalized_at = None
        summary.finalized_by = None
        summary.finalized_comment = None

    return from_status
