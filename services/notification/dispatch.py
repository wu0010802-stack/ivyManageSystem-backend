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
from dataclasses import dataclass, replace as _dc_replace
from typing import Optional

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from models.base import get_session_factory
from models.database import NotificationLog, NotificationPreference
from services.notification._channels.line import LineAdapter
from services.notification._channels.ws import WsAdapter, _inbox_ws_push
from services.notification.channel_matrix import CHANNEL_MATRIX, Channel
from services.notification.event_types import NOTIFICATION_EVENT_TYPES
from services.notification.renderers import render

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
    # Phase 4 Section 2: LINE 群組推送 mode。設值時 LINE adapter 不走
    # _resolve_line_user_id，改用 push_text_to_group(group_id, text) 推群組。
    # 與 recipient_user_id 二擇一（或併用：個人 in_app + 群組 LINE 都會跑）。
    line_group_id: Optional[str] = None


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
    line_group_id: Optional[str] = None,
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
        line_group_id: Phase 4 Section 2 新加 — LINE 群組推送 mode。設值時
            LINE adapter 走 push_text_to_group(group_id, text)，跳過
            _resolve_line_user_id 個人解析。dismissal.created 等群組事件用。
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
            line_group_id=line_group_id,
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


# ────────────────────── Fan-out ──────────────────────

# Adapter singletons（lazy-init；main.py 啟動後 LineAdapter 注入 LineService instance）
_line_adapter: LineAdapter | None = None
_ws_adapter: WsAdapter | None = None


def _get_line_adapter() -> LineAdapter:
    global _line_adapter
    if _line_adapter is None:
        # 從既有 line_service singleton 取（main.py: `line_service = LineService()`）
        from main import line_service  # lazy 避免循環 import

        _line_adapter = LineAdapter(line_service)
    return _line_adapter


def _get_ws_adapter() -> WsAdapter:
    global _ws_adapter
    if _ws_adapter is None:
        _ws_adapter = WsAdapter()
    return _ws_adapter


def _pref_enabled(session, user_id, event_type: str, channel: str) -> bool:
    """偏好 gate：缺 row = True；row 存在看 enabled 欄。

    無 recipient（群組推播）視為 enabled（gate 不適用）。
    DB 異常 fail-closed 沿用既有 should_push_to_parent 慣例。
    """
    if user_id is None:
        return True
    try:
        row = (
            session.query(NotificationPreference)
            .filter(
                NotificationPreference.user_id == user_id,
                NotificationPreference.event_type == event_type,
                NotificationPreference.channel == channel,
            )
            .first()
        )
        if row is None:
            return True
        return bool(row.enabled)
    except Exception as exc:
        logger.warning("_pref_enabled failed (fail-closed): %s", exc)
        return False


def _resolve_line_user_id(session, user_id) -> str | None:
    """User.id → User.line_user_id（active + line_follow_confirmed 才回）。

    沿用 line_service.should_push_to_parent 的可達性檢查；fail-closed。
    用於 _fan_out 在 call LINE adapter 前 pre-resolve；caller 仍用 int User.id。
    """
    if user_id is None:
        return None
    try:
        from models.database import User

        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            return None
        if not user.line_user_id or not user.line_follow_confirmed_at:
            return None
        return user.line_user_id
    except Exception as exc:
        logger.warning("_resolve_line_user_id failed (fail-closed): %s", exc)
        return None


def _fan_out(evt: PendingEvent) -> None:
    """tx commit 後實際發送：寫 log → 過 gate → 呼叫 adapter。

    任何 channel 失敗只記 channels_failed，不 re-raise。
    """
    log_session = get_session_factory()()
    try:
        rendered = render(evt.event_type, evt.context)

        # 篩 active channels：in_app 必（matrix 有就一定走）；line/ws 過 pref gate
        active_channels: list[str] = []
        for ch in evt.channels:
            if ch == "in_app":
                active_channels.append(ch)
            elif _pref_enabled(log_session, evt.recipient_user_id, evt.event_type, ch):
                active_channels.append(ch)

        # 只有 in_app 在 matrix 內才寫 log row（家長域沒 in_app 就不寫，但仍跑 line/ws）
        log_id: int | None = None
        if "in_app" in evt.channels:
            log_row = NotificationLog(
                recipient_user_id=evt.recipient_user_id,
                event_type=evt.event_type,
                sender_id=evt.sender_id,
                source_entity_type=evt.source_entity_type,
                source_entity_id=evt.source_entity_id,
                title=rendered.title,
                body=rendered.body,
                payload_json=dict(evt.context),
                deep_link=rendered.deep_link,
                channels_attempted=list(active_channels),
                channels_succeeded=["in_app"],
                channels_failed=[],
            )
            log_session.add(log_row)
            log_session.commit()
            log_id = log_row.id

            # in_app 路徑後立刻推 inbox WS；失敗只 warning 不算 in_app failure
            if evt.recipient_user_id is not None:
                try:
                    _inbox_ws_push(evt, rendered, log_id)
                except Exception as exc:
                    logger.warning(
                        "inbox WS push 失敗 log_id=%s event=%s: %s",
                        log_id,
                        evt.event_type,
                        exc,
                    )

        # 跑 line / ws adapter（in_app 已處理過跳過）
        succeeded: list[str] = []
        failed: list[dict] = []
        for ch in active_channels:
            if ch == "in_app":
                continue
            if ch == "line":
                # Phase 4 Section 2: line_group_id 設值 → 群組 mode，LineAdapter
                # 走 push_text_to_group；不設 → 個人 mode，pre-resolve user_id。
                if evt.line_group_id is not None:
                    line_evt = evt  # group mode 不替換 recipient_user_id
                else:
                    line_user_id = _resolve_line_user_id(
                        log_session, evt.recipient_user_id
                    )
                    if line_user_id is None:
                        failed.append({"channel": "line", "error": "unreachable_user"})
                        continue
                    line_evt = _dc_replace(evt, recipient_user_id=line_user_id)
                try:
                    _get_line_adapter().send(line_evt, rendered, log_id=log_id or 0)
                    succeeded.append("line")
                except Exception as exc:
                    logger.exception(
                        "LINE channel failed event=%s user=%s group=%s",
                        evt.event_type,
                        evt.recipient_user_id,
                        evt.line_group_id,
                    )
                    failed.append({"channel": "line", "error": type(exc).__name__})
                continue
            # ws or others
            adapter = _get_ws_adapter()
            try:
                adapter.send(evt, rendered, log_id=log_id or 0)
                succeeded.append(ch)
            except Exception as exc:
                logger.exception(
                    "channel %s failed event=%s recipient=%s",
                    ch,
                    evt.event_type,
                    evt.recipient_user_id,
                )
                failed.append({"channel": ch, "error": type(exc).__name__})

        # 更新 log row 的 channels_succeeded / channels_failed（若有寫 log）
        if log_id is not None and (succeeded or failed):
            row = log_session.query(NotificationLog).get(log_id)
            if row is not None:
                row.channels_succeeded = list(row.channels_succeeded) + succeeded
                row.channels_failed = list(row.channels_failed) + failed
                log_session.commit()
    finally:
        log_session.close()
