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

# S4（2026-06-13）：SQLAlchemy StatementError 的 `[parameters: {'key': 'value'}]`
# 是 `'key': 'value'`（repr dict）格式，不是 key=value，舊 regex 不命中 →
# DB 例外 log 直接洩 PII。補一條 quoted-key regex：
#   - key 被單/雙引號包住（(?P=kq) backreference 保證前後同款引號）
#   - value 為帶引號字串（容許跳脫字元）或裸 token（數字 / None 等）
_QUOTED_KEY_VALUE_RE = re.compile(
    r"""(?P<kq>['"])(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)(?P=kq)\s*:\s*"""
    r"""(?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[^\s,，。}\]]+)"""
)


def _redact_string(s: str) -> str:
    """對 string 內 key=value 與 'key': 'value' 兩種 pattern，
    若 key 命中 PII denylist 替換 value 為 [Filtered]。"""

    def _replace(m: re.Match) -> str:
        key = m.group("key")
        if _key_is_pii(key):
            return f"{key}={_FILTERED}"
        return m.group(0)

    def _replace_quoted(m: re.Match) -> str:
        key = m.group("key")
        if _key_is_pii(key):
            kq = m.group("kq")
            return f"{kq}{key}{kq}: {_FILTERED}"
        return m.group(0)

    s = _KEY_VALUE_RE.sub(_replace, s)
    return _QUOTED_KEY_VALUE_RE.sub(_replace_quoted, s)


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

    設計原則（advisor 2026-06-01 修正）：
    - 有 args 時：先 _scrub_mapping 遮 args 內 dict PII，再 getMessage() format，
      對 formatted 結果跑 _redact_string，最後清 record.args（handler 不再 format）。
      Why: 舊版對 raw format string redact 會把 `key=%s` 的 %s placeholder 抹掉，
      handler `msg % args` placeholder 數 < args 數 → TypeError → 500；且 positional
      PII（如 guardian_id=%s 的真實值）實際未遮。format 後再 redact 兩者皆解。
    - 無 args 時：msg 為 literal，直接對 raw string redact。
    - exc_info exception args 做 string-level redaction
    - fail-safe：任何例外都不擋 record（return True 保住 log pipeline）
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1+2. format → redact → clear args（advisor 2026-06-01）
        # 先把 args 內 dict PII 遮掉，再 getMessage() format（此時 positional 值已是
        # 真實值），對結果 string 跑 _redact_string，最後清 args 讓 handler 不再 format
        # （避免重複 format 與 placeholder/args 數不符的 TypeError）。
        try:
            if isinstance(record.msg, str):
                if record.args:
                    try:
                        record.args = _redact_args(record.args)
                        record.msg = _redact_string(record.getMessage())
                        record.args = None
                    except Exception:  # noqa: BLE001
                        # getMessage() 對真正 malformed 的呼叫會 raise（與 redaction 無關）；
                        # fallback 回 raw-msg redact + 保留 args（同既有 safe state：
                        # handler handleError 印的是已遮的 format string，不外洩 PII）。
                        record.msg = _redact_string(record.msg)
                else:
                    record.msg = _redact_string(record.msg)
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
