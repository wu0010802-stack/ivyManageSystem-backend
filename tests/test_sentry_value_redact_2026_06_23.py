"""P2-2 / P2-10 回歸（2026-06-23 全系統資安掃描）：Sentry value-level PII 遮罩。

Sentry 遮罩原為純 key-based（_scrub_mapping 只遮命中 denylist 的 key、string value
原樣保留），三條 value-level 缺口：
- P2-2：DB 例外訊息 event['exception']['values'][].value 含 SQL bind 參數
  （[parameters: {'phone':..., 'id_number':...}]）完全不被 before_send 遮。
- P2-10：自由文字 value（reason/note/teacher_note/summary）內的手機/身分證/市話
  在 extra/contexts 不被遮（key 非 PII → value 漏網）。

修法：sentry_init 加 _redact_pii_value（身分證/手機/市話正則，對齊 audit_redact），
_scrub_mapping 對 string value 跑一層、_scrub_event 對 exception.value 跑一層。
"""

from __future__ import annotations

from utils.sentry_init import _redact_pii_value, _scrub_event, _scrub_mapping

# ── P2-10：自由文字 value-level 遮罩 ──


def test_redact_pii_value_masks_mobile_id_landline():
    out = _redact_pii_value("電話 0912345678 / 身分證 A123456789 / 市話 02-12345678")
    assert "0912345678" not in out
    assert "A123456789" not in out
    assert "02-12345678" not in out


def test_redact_pii_value_keeps_plain_text():
    """非識別子文字（操作描述/數字 id）不應被誤遮。"""
    assert _redact_pii_value("學生編號 12345 已報名") == "學生編號 12345 已報名"


def test_scrub_mapping_redacts_pii_in_free_text_value():
    """非 PII key（reason/note）下含手機/身分證的自由文字 value 應被遮。"""
    result = _scrub_mapping(
        {"reason": "家長電話 0912345678", "note": "身分證 A123456789"}
    )
    assert "0912345678" not in result["reason"]
    assert "A123456789" not in result["note"]


def test_scrub_mapping_still_filters_pii_key_and_keeps_plain():
    """既有 key-based 遮罩不變；非 PII 純文字保留。"""
    result = _scrub_mapping({"phone": "0912345678", "name": "Alice", "count": 42})
    assert result["phone"] == "[Filtered]"  # key 命中
    assert result["name"] == "Alice"  # 非 PII 純文字保留
    assert result["count"] == 42


# ── P2-2：DB 例外訊息 value 遮罩 ──


def test_scrub_event_redacts_exception_value_sql_params():
    event = {
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": (
                        "duplicate key value violates unique constraint "
                        "[parameters: {'phone': '0912345678', "
                        "'id_number': 'A123456789'}]"
                    ),
                }
            ]
        }
    }
    out = _scrub_event(event)
    v = out["exception"]["values"][0]["value"]
    assert "0912345678" not in v, "例外訊息中的手機應被遮"
    assert "A123456789" not in v, "例外訊息中的身分證應被遮"


def test_scrub_event_exception_missing_is_safe():
    """無 exception 區塊不應炸。"""
    out = _scrub_event({"message": "hello"})
    assert out["message"] == "hello"
