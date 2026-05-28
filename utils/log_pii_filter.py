"""utils/log_pii_filter.py

PII redaction filter for logging records.

對 LogRecord.msg raw format string + record.args (dict) + record.exc_info
做 key-based PII detection 與 value replacement。

重用 utils.sentry_init._key_is_pii 與 _scrub_mapping 保證與 Sentry event
denylist 同步。新增 PII 欄位只改一處 (sentry_init.py _PII_KEY_SUBSTRINGS)。
"""

import logging
import re
from typing import Any

from utils.sentry_init import _FILTERED, _key_is_pii, _scrub_mapping

# 抓 key=value 形式（key 為英數+底線、value 為非空白/非中文標點字串到下一個空白）
# 例：student_id=42 / student_name=小明 / phone=0912-345-678
# 不抓 key 含空白 / value 跨多 token 的 case（保守 redaction 避免破壞 log debug）
_KEY_VALUE_RE = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>[^\s,，。]+)")


def _redact_string(s: str) -> str:
    """對 string 內所有 key=value pattern，若 key 命中 PII denylist 替換 value 為 [Filtered]。"""

    def _replace(m: re.Match) -> str:
        key = m.group("key")
        if _key_is_pii(key):
            return f"{key}={_FILTERED}"
        return m.group(0)

    return _KEY_VALUE_RE.sub(_replace, s)


def _redact_args(args: Any) -> Any:
    """對 record.args 做 redaction。

    - dict: 走 _scrub_mapping 遞迴遮 PII keys
    - tuple/list: element 若 dict 走 _scrub_mapping；其他元素不動
      （format string 的 positional args 沒法跟 key name 對應，
       已透過 record.msg raw regex 對 format string 做掃描）
    """
    if isinstance(args, dict):
        return _scrub_mapping(args)
    if isinstance(args, (tuple, list)):
        return type(args)(
            _scrub_mapping(a) if isinstance(a, (dict, list)) else a for a in args
        )
    return args


class PIIRedactionFilter(logging.Filter):
    """對 LogRecord 做 PII redaction（msg / args / exc_info 三層）。

    Attach 到 logging.root handler (main.py:_configure_logging) 後，所有
    logger 出來的 record 都會走過此 filter，msg 與 args 經 redact 才到 handler。

    設計原則（advisor 2026-05-28）：
    - 對 raw record.msg 跑 regex scrub，不 call getMessage()，
      避免 format mismatch 時 try/except swallow PII
    - args 內 dict 走 _scrub_mapping 補完整
    - exc_info exception args 做 string-level redaction
    - fail-safe：任何例外都不擋 record（return True 保住 log pipeline）
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. record.msg raw format string 直接 regex scrub
        # advisor 2026-05-28：不 call getMessage() 避免 format mismatch swallow PII。
        # 對 raw msg scrub 已 cover format string 內的 PII key（key 通常寫在 format
        # string 而非 args 中）；args 仍 mutable 給 handler format，step 2 對
        # args 內 dict 走 _scrub_mapping 補完整。
        try:
            if isinstance(record.msg, str):
                redacted = _redact_string(record.msg)
                if redacted != record.msg:
                    record.msg = redacted
        except Exception:  # noqa: BLE001
            pass

        # 2. record.args 若是 dict/list 做 _scrub_mapping
        try:
            if record.args:
                record.args = _redact_args(record.args)
        except Exception:  # noqa: BLE001
            pass

        # 3. exc_info 含 exception，args 內可能有 PII (e.g. SQL value)
        # 對 exception.args 做 string-level redaction
        try:
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                if hasattr(exc, "args") and exc.args:
                    new_args = tuple(
                        _redact_string(a) if isinstance(a, str) else a for a in exc.args
                    )
                    if new_args != exc.args:
                        exc.args = new_args
        except Exception:  # noqa: BLE001
            pass

        return True  # 不擋 record，純改寫
