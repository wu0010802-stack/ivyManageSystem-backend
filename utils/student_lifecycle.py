"""Student lifecycle 變更原子化 helper。

所有 `Student.lifecycle_status` 變更必須走 set_lifecycle_status，不可直接
.lifecycle_status =。理由：(1) 維護 terminal_entered_at 戳記給 PII retention
GC 算 365 天 (2) 統一寫 audit_log (3) 復學自動取消 retention timer。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from models.audit import AuditLog
from models.classroom import (
    Student,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)

if TYPE_CHECKING:
    from starlette.requests import Request

_TERMINAL_LIFECYCLE = frozenset(
    {LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN}
)


def set_lifecycle_status(
    session,
    student: Student,
    new_status: str,
    *,
    actor_user_id: int | None = None,
    audit: bool = True,
    reason: str | None = None,
    request: "Request | None" = None,
) -> None:
    """原子化變更 lifecycle_status + 維護 terminal_entered_at + 寫 audit_log。

    - 非終態 → 終態：terminal_entered_at = NOW(utc)
    - 終態 → 非終態（罕見復學）：terminal_entered_at = NULL（取消 retention）
    - 終態 → 終態 / 非終態 → 非終態：戳記不動
    - 同狀態：no-op（不寫 audit_log）
    """
    old_status = student.lifecycle_status
    if old_status == new_status:
        return

    was_terminal = old_status in _TERMINAL_LIFECYCLE
    is_terminal = new_status in _TERMINAL_LIFECYCLE

    student.lifecycle_status = new_status
    if not was_terminal and is_terminal:
        student.terminal_entered_at = datetime.now(timezone.utc)
    elif was_terminal and not is_terminal:
        student.terminal_entered_at = None
    # 終態 → 終態 或 非終態 → 非終態：戳記不動

    # 非終態 → 終態 且有 HTTP request context：顯式標記軟刪，讓 AuditMiddleware
    # 產出 "軟刪 學生 xxx" summary（業務語意是「離開系統」而非 UPDATE）。
    # request=None 的 caller（排程器 / 批量遷移）由 if 守衛自動略過，不影響現有呼叫。
    if not was_terminal and is_terminal and request is not None:
        from utils.audit import mark_soft_delete

        mark_soft_delete(request, "student", student.name or f"#{student.id}")

    if audit:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                username="scheduler" if actor_user_id is None else None,
                action="UPDATE",
                entity_type="student",
                entity_id=str(student.id),
                summary=f"lifecycle: {old_status} → {new_status}",
                changes=json.dumps(
                    {
                        "old_status": old_status,
                        "new_status": new_status,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                ip_address=None,
                created_at=datetime.now(),  # noqa: DTZ005
            )
        )
