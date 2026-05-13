"""統一審核結果 LINE 通知入口。

Why centralize: 三 router 原本各自呼叫 LineService.notify_*_result，時序與 reason 帶入不一致。
本入口由 caller 在 commit 後呼叫，內部 dispatch 並 swallow LineService 例外。
"""

import logging
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

DocType = Literal["leave", "overtime", "punch_correction"]
Action = Literal["approve", "reject"]


def notify_approval(
    *,
    line_service: Optional[Any],
    doc_type: DocType,
    action: Action,
    line_user_id: Optional[str],
    name: str,
    context: dict,
    rejection_reason: Optional[str] = None,
) -> None:
    """非阻塞通知。caller 必須在 DB commit 後呼叫。"""
    if line_service is None or not line_user_id:
        return
    approved = action == "approve"
    try:
        if doc_type == "leave":
            line_service.notify_leave_result(
                line_user_id,
                name,
                context["leave_type"],
                context["start"],
                context["end"],
                approved,
                rejection_reason,
            )
        elif doc_type == "overtime":
            line_service.notify_overtime_result(
                line_user_id,
                name,
                context["ot_date"],
                context["ot_type"],
                approved,
            )
        elif doc_type == "punch_correction":
            line_service.notify_punch_correction_result(
                line_user_id,
                name,
                context["target_date"],
                approved,
                rejection_reason,
            )
    except Exception as exc:
        logger.warning(
            "LINE notify_approval 失敗（doc_type=%s action=%s user=%s）：%s",
            doc_type,
            action,
            line_user_id,
            exc,
        )
