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

from sqlalchemy.orm import Session

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
