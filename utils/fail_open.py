"""Fail-open 共用 observability helper。

把散落在 utils/auth.py、utils/rate_limit.py、utils/rate_limit_db.py
等處的「logger.warning + return False/None」fail-open 集中成同一 helper：
保留 fail-open 行為（不擋請求避免 DB 抖動全站 down），但加 Sentry tag
+ capture_exception 讓 ops 在 DB 大範圍失聯時看得到。
"""

import logging
from typing import Any

import sentry_sdk

logger = logging.getLogger(__name__)


def capture_fail_open(operation: str, error: Exception, **extra: Any) -> None:
    """記錄 fail-open 事件並送 Sentry。

    Args:
        operation: fail-open 點識別字串，格式 `{module}.{function}`（如
            "is_token_revoked"、"rate_limit_db.bump_failed_login"）。
            穩定字串用於 Sentry dashboard filter/alert rule。
        error: 觸發 fail-open 的 exception
        **extra: 額外 tag context（如 key、jti、name），以
            `fail_open.{key}` 形式設成 Sentry tag。限 primitive value
            (str/int/bool)；dict/list 等複合型別請呼叫端先序列化。
    """
    logger.warning("%s 失敗，fail-open: %s", operation, error)
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("fail_open", operation)
        for k, v in extra.items():
            scope.set_tag(f"fail_open.{k}", str(v))
        sentry_sdk.capture_exception(error)
