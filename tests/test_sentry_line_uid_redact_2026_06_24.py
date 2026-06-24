"""SEC-2026-0624-01 縱深防禦回歸：Sentry value-level 遮罩涵蓋 LINE userId。

家長綁定流程曾以完整 LINE userId（`U` + 32 hex，可直接對映真實 LINE 帳號的
個資）寫進 log；若該字串隨自由文字進到 Sentry breadcrumb message / exception
value，現有 `_redact_pii_value`（只有身分證/手機/市話正則）不會遮。

修法：`_redact_pii_value` 增 LINE userId 正則；breadcrumb message 也跑
`_redact_pii_value`（原本只跑 `_sanitize_url`）。前端 src/utils/sentry.ts 同步。
"""

from __future__ import annotations

from utils.sentry_init import _redact_pii_value, _scrub_breadcrumb, _scrub_event

_LINE_UID = "U" + "0123456789abcdef0123456789abcdef"  # 真實格式：U + 32 hex


def test_redact_pii_value_masks_line_user_id():
    out = _redact_pii_value(f"綁定成功 line_user_id={_LINE_UID} 完成")
    assert _LINE_UID not in out
    assert "[Filtered]" in out


def test_redact_pii_value_keeps_short_u_prefixed_token():
    """非 LINE-uid 格式（如 U_victim_parent_001）不應被此正則誤遮。"""
    assert _redact_pii_value("使用者 U_victim_parent_001 已綁定") == (
        "使用者 U_victim_parent_001 已綁定"
    )


def test_scrub_event_redacts_line_uid_in_breadcrumb_message():
    event = {
        "breadcrumbs": {
            "values": [
                {"message": f"[parent-bind] line_user_id={_LINE_UID}"},
            ]
        }
    }
    out = _scrub_event(event)
    msg = out["breadcrumbs"]["values"][0]["message"]
    assert _LINE_UID not in msg


def test_scrub_breadcrumb_redacts_line_uid_in_message():
    crumb = {"message": f"bind ok {_LINE_UID}"}
    out = _scrub_breadcrumb(crumb)
    assert _LINE_UID not in out["message"]
