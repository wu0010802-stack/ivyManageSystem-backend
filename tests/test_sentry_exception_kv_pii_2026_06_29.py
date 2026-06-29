"""Track 8 BE — qa-loop round2（2026-06-29）：DB 例外 [parameters: {...}] 內的 key-based PII。

_scrub_event 對 exception.values[].value 原本只跑 value-level 正則（身分證/手機/市話/LINE id），
攔不到 email / 姓名 / 銀行帳號 等須靠 key 判定的 PII。SQLAlchemy IntegrityError/StatementError
的 str 預設含 `[parameters: {'email': ..., 'parent_name': ..., 'bank_account': ...}]` → 原文
進 Sentry。修法：對 exception value 額外套用 key-value denylist（_redact_pii_kv_in_text）。
"""

from __future__ import annotations

from utils.sentry_init import _scrub_event, _redact_pii_kv_in_text


def test_kv_redact_filters_denylist_key_values():
    text = "{'email': 'foo@bar.com', 'parent_name': '王小明', 'bank_account': '1234567890'}"
    out = _redact_pii_kv_in_text(text)
    assert "foo@bar.com" not in out
    assert "王小明" not in out
    assert "1234567890" not in out
    assert "[Filtered]" in out
    # key 本身保留（供 debug 知道是哪個欄位衝突），只遮 value
    assert "email" in out and "parent_name" in out


def test_kv_redact_keeps_non_pii_values():
    text = "{'count': 5, 'status': 'ok'}"
    out = _redact_pii_kv_in_text(text)
    assert "5" in out
    assert "ok" in out
    assert "[Filtered]" not in out


def test_scrub_event_redacts_db_parameters_pii():
    event = {
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": (
                        "duplicate key value violates unique constraint "
                        "[parameters: {'email': 'foo@bar.com', 'parent_name': '王小明'}]"
                    ),
                }
            ]
        }
    }
    out = _scrub_event(event)
    v = out["exception"]["values"][0]["value"]
    assert "foo@bar.com" not in v
    assert "王小明" not in v
    assert "[Filtered]" in v
