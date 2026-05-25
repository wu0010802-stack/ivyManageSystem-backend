"""通知中央 dispatcher：唯一對外入口 + after_commit 自動 fan-out。

Lifecycle：
1. caller `dispatch.enqueue(session=..., event_type=..., ...)`
   → 事件註冊到 session.info[_QUEUE_KEY]，**尚未發送**
2. caller `session.commit()` 觸發 SQLAlchemy after_commit listener
   → `_drain_after_commit(session)` 拉出 queue 逐筆 `_fan_out`
3. `_fan_out`：開 short-lived session 寫 notification_logs row
   → 過 preference gate（in_app 不過）
   → in_app 路徑同時 _inbox_ws_push 給 recipient
   → 呼叫 line/ws adapter
4. caller `session.rollback()` 觸發 `_clear_on_rollback` 清空 queue

任何 fan-out 失敗只 log + 寫入 channels_failed，絕不 re-raise（業務 tx 已 commit）。

session 必須來自 models.base.get_session_factory()，parent_db / spike_rls
等其他 factory 不受監聽。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from services.notification.channel_matrix import CHANNEL_MATRIX, Channel
from services.notification.event_types import NOTIFICATION_EVENT_TYPES

logger = logging.getLogger(__name__)

_QUEUE_KEY = "ivy_notification_queue"


@dataclass(frozen=True)
class PendingEvent:
    event_type: str
    recipient_user_id: Optional[int]
    context: dict
    sender_id: Optional[int]
    source_entity_type: Optional[str]
    source_entity_id: Optional[int]
    channels: tuple[Channel, ...]


def enqueue(
    session: Session,
    *,
    event_type: str,
    recipient_user_id: Optional[int],
    context: dict,
    sender_id: Optional[int] = None,
    source_entity_type: Optional[str] = None,
    source_entity_id: Optional[int] = None,
    channels_override: Optional[tuple[Channel, ...]] = None,
) -> None:
    """註冊一筆通知事件到當前 session 的 queue。

    tx commit 後由 after_commit hook 自動 fan-out（in_app log + LINE + WS）。
    rollback 則自動丟棄（透過 after_rollback hook）。

    Args:
        session: 主庫 session（必須來自 models.base.get_session_factory()）
        event_type: 必須在 NOTIFICATION_EVENT_TYPES 內，否則 ValueError
        recipient_user_id: 接收者 user_id；None 表群組推播（如 dismissal.created）
        context: renderer 用的 dict，會被淺拷貝
        sender_id: 觸發者 user_id（顯示「誰發的」）
        source_entity_type / source_entity_id: 反查源頭；未來 outbox idempotency key
        channels_override: 罕用，特殊 case 覆蓋 CHANNEL_MATRIX
    """
    if event_type not in NOTIFICATION_EVENT_TYPES:
        raise ValueError(f"未知 event_type: {event_type}")
    channels = channels_override or CHANNEL_MATRIX.get(event_type, ())
    if not channels:
        logger.debug("event_type %s 無 channel 設定，略過", event_type)
        return
    # 確保 session 有開啟真實 transaction，讓 after_rollback hook 能在 rollback 時觸發。
    # （session.info 寫入不觸發 SQLAlchemy autobegin；notification 應與業務 tx 同生命週期。）
    if not session.in_transaction():
        session.begin()
    queue = session.info.setdefault(_QUEUE_KEY, [])
    queue.append(
        PendingEvent(
            event_type=event_type,
            recipient_user_id=recipient_user_id,
            context=dict(context),
            sender_id=sender_id,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            channels=channels,
        )
    )


# ────────────────────── Hooks ──────────────────────

_HOOKS_INSTALLED: set[sessionmaker] = set()


def install_session_hooks(factory: sessionmaker) -> None:
    """把 after_commit / after_rollback listener 綁到指定 session factory。

    Idempotent — 重複呼叫對同一 factory 不會綁多次。

    必須在 app startup（main.py lifespan）呼叫一次，傳入 models.base.get_session_factory()。
    test fixture 在 swap factory 後也須再呼叫一次以綁到 test factory。
    """
    if factory in _HOOKS_INSTALLED:
        return
    event.listen(factory, "after_commit", _drain_after_commit)
    event.listen(factory, "after_rollback", _clear_on_rollback)
    _HOOKS_INSTALLED.add(factory)


def _drain_after_commit(session: Session) -> None:
    pending = session.info.pop(_QUEUE_KEY, None)
    if not pending:
        return
    for evt in pending:
        try:
            _fan_out(evt)
        except Exception:
            logger.exception(
                "dispatch fan-out 失敗 event=%s recipient=%s",
                evt.event_type,
                evt.recipient_user_id,
            )
            # 絕不 re-raise — 一筆 fan-out 失敗不能影響後續


def _clear_on_rollback(session: Session) -> None:
    session.info.pop(_QUEUE_KEY, None)


def _fan_out(evt: PendingEvent) -> None:
    """Task 11 實作；現為 stub 讓 hook 測試可 mock。"""
    raise NotImplementedError("Task 11 實作")
