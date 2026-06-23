"""Fail-open 共用 observability helper。

把散落在 utils/auth.py、utils/rate_limit.py、utils/rate_limit_db.py
等處的「logger.warning + return False/None」fail-open 集中成同一 helper：
保留 fail-open 行為（不擋請求避免 DB 抖動全站 down），但加 Sentry tag
+ capture_exception 讓 ops 在 DB 大範圍失聯時看得到。
"""

import logging
from typing import Any

import sentry_sdk

from utils.sentry_init import _hash_user_id, _redact_pii_value

logger = logging.getLogger(__name__)

# P2-3（2026-06-23 資安掃描）：這些 extra key 的 value 可能是識別子
# （rate-limit key = username / IP / line_user_id；jti = token id），以 hash 取代
# 明文進 Sentry tag（保留 grouping、移除直連 PII）。其餘 key（name / scope /
# namespace 等非 PII context）保留明文但仍跑 value-level 識別子遮罩兜底。
_FAIL_OPEN_HASH_KEYS = frozenset({"key", "jti"})


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
            if k in _FAIL_OPEN_HASH_KEYS:
                scope.set_tag(f"fail_open.{k}", _hash_user_id(str(v)))
            else:
                scope.set_tag(f"fail_open.{k}", _redact_pii_value(str(v)))
        sentry_sdk.capture_exception(error)
