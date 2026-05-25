"""LINE channel adapter — thin dispatch 到既有 line_service.notify_* method。

Phase 1：LINE_HANDLERS 為空 dict，所有 event 走 fallback push_text_to_user
（純 text）。Phase 2 PR-A 開始按 router 遷移時為每個 event 註冊對映 handler
（function(line_service, evt, rendered) -> None），讓 LINE Flex / quick reply
等複雜推送繼續用既有 line_service method。

Phase 4 完成時 line_service 重構為純 builder + 一個 push 入口，本檔 LINE_HANDLERS
不再需要。
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# event_type → handler(line_service, evt, rendered)
LINE_HANDLERS: dict[str, Callable] = {}


class LineAdapter:
    def __init__(self, line_service):
        self._ls = line_service

    def send(self, evt, rendered, *, log_id: int) -> None:
        # log_id 留作 Phase 4 push receipt 追蹤；v1 不用
        handler = LINE_HANDLERS.get(evt.event_type)
        if handler is None:
            text = (rendered.title or "") + (
                "\n" + rendered.body if rendered.body else ""
            )
            if evt.recipient_user_id is None:
                logger.warning(
                    "LINE adapter fallback：event=%s 無 recipient_user_id 跳過",
                    evt.event_type,
                )
                return
            # 注意：recipient_user_id 在 enqueue 時是 int (User.id)，但 LineService
            # 需 LINE user_id (str)。Phase 1 fallback 暫時只支援 str；Phase 2 caller
            # 遷移時 _fan_out 會 pre-resolve User.id → user.line_user_id 並傳 str。
            if not isinstance(evt.recipient_user_id, str):
                logger.warning(
                    "LINE adapter fallback：recipient_user_id 非 LINE user_id (int=%s) 跳過",
                    evt.recipient_user_id,
                )
                return
            self._ls.push_text_to_user(evt.recipient_user_id, text)
            return
        handler(self._ls, evt, rendered)
