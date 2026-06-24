"""tests/test_audit_redact.py — P0b audit PII redaction 純函式測試。

Refs: docs/superpowers/specs/2026-05-28-audit-pii-redact-retention-design.md §4.1
"""

from __future__ import annotations

from utils.audit_redact import _FILTERED, redact_pii, redact_pii_text

# ── 基本遮罩 ──


def test_id_number_is_redacted():
    out = redact_pii({"id_number": "A123456789"})
    assert out == {"id_number": _FILTERED}


def test_phone_is_redacted():
    out = redact_pii({"phone": "0912345678"})
    assert out == {"phone": _FILTERED}


def test_address_is_redacted():
    out = redact_pii({"address": "台北市信義區"})
    assert out == {"address": _FILTERED}


def test_email_is_redacted():
    out = redact_pii({"email": "p@example.com"})
    assert out == {"email": _FILTERED}


# ── Finding H：自由文字 summary 的強識別子遮罩（redact_pii_text）──


def test_text_masks_tw_national_id():
    out = redact_pii_text("家長申請刪除 身分證 A123456789 之資料")
    assert "A123456789" not in out
    assert _FILTERED in out


def test_text_masks_mobile_phone():
    out = redact_pii_text("聯絡電話 0912345678 已更新")
    assert "0912345678" not in out
    assert _FILTERED in out


def test_text_masks_landline_with_dash():
    out = redact_pii_text("市話 02-12345678 變更")
    assert "02-12345678" not in out


def test_text_keeps_operational_ids_and_names():
    """admin-only 稽核面需保留可讀性：純數字操作 ID 與姓名不得被誤遮。"""
    s = "操作者 林佳穎（user_id=123）切換為 employee_id=45，金額 NT$50000"
    out = redact_pii_text(s)
    assert out == s  # 無強識別子 → 原樣保留


def test_text_masks_cjk_adjacent_identifiers():
    """中文緊鄰數字無空白也應遮（與 sentry_init._redact_pii_value 對齊；原 \\b 漏遮）。"""
    out = redact_pii_text("電話0912345678請改期，身分證A123456789止")
    assert "0912345678" not in out
    assert "A123456789" not in out


def test_text_not_masked_inside_longer_digit_run():
    """保留原意：夾在更長數字串中的子序列不誤遮。"""
    assert redact_pii_text("代碼1230912345678末") == "代碼1230912345678末"


def test_text_handles_none_and_nonstr():
    assert redact_pii_text(None) is None
    assert redact_pii_text("") == ""


# ── Amount 保留（audit 例外）──


def test_salary_amount_is_kept_for_audit():
    """金流 audit 必須保留 amount 值才有稽核價值"""
    out = redact_pii({"salary_amount": 50000})
    assert out == {"salary_amount": 50000}


def test_bonus_amount_is_kept():
    out = redact_pii({"bonus_amount": 10000})
    assert out == {"bonus_amount": 10000}


def test_amount_due_is_kept():
    out = redact_pii({"amount_due": 1500})
    assert out == {"amount_due": 1500}


# ── Sentry exempt 不遮（系統 metadata 欄位）──


def test_ip_address_is_not_redacted():
    """ip_address 雖含 'address' substring 但屬系統欄位"""
    out = redact_pii({"ip_address": "1.2.3.4"})
    assert out == {"ip_address": "1.2.3.4"}


def test_health_check_is_not_redacted():
    out = redact_pii({"health_check_status": "ok"})
    assert out == {"health_check_status": "ok"}


def test_email_template_is_not_redacted():
    out = redact_pii({"email_template_id": 42})
    assert out == {"email_template_id": 42}


# ── 識別子仍遮（在 finance 範疇但是識別子）──


def test_bank_account_is_redacted():
    out = redact_pii({"bank_account": "1234-5678"})
    assert out == {"bank_account": _FILTERED}


def test_card_no_is_redacted():
    out = redact_pii({"card_no": "4111-1111"})
    assert out == {"card_no": _FILTERED}


# ── 巢狀 dict / list ──


def test_nested_dict_recursive():
    out = redact_pii({"student": {"id_number": "A1", "name": "小明"}})
    assert out == {
        "student": {
            "id_number": _FILTERED,
            "name": "小明",  # name 不在 denylist
        }
    }


def test_list_recursive():
    out = redact_pii([{"phone": "1"}, {"name": "x"}])
    assert out == [{"phone": _FILTERED}, {"name": "x"}]


def test_before_after_diff_redacts_at_key_level():
    """{"phone": {"before": x, "after": y}} 整個 value 遮（不暴露 nested PII）"""
    out = redact_pii({"phone": {"before": "0912", "after": "0987"}})
    assert out == {"phone": _FILTERED}


def test_deep_nested_5_levels():
    deep = {"a": {"b": {"c": {"d": {"id_number": "X"}}}}}
    out = redact_pii(deep)
    assert out["a"]["b"]["c"]["d"]["id_number"] == _FILTERED


# ── 混合（部分遮部分保留）──


def test_mixed_redact_and_keep():
    out = redact_pii(
        {
            "salary_amount": 50000,
            "bank_account": "1234",
            "username": "alice",
            "ip_address": "1.2.3.4",
        }
    )
    assert out == {
        "salary_amount": 50000,
        "bank_account": _FILTERED,
        "username": "alice",
        "ip_address": "1.2.3.4",
    }


# ── 保留 key（給 audit 仍能看到「哪個欄位被改」）──


def test_redaction_preserves_all_keys():
    inp = {"phone": "X", "email": "Y", "name": "Z"}
    out = redact_pii(inp)
    assert set(out.keys()) == set(inp.keys())


# ── Edge cases ──


def test_none_input():
    assert redact_pii(None) is None


def test_empty_dict():
    assert redact_pii({}) == {}


def test_empty_list():
    assert redact_pii([]) == []


def test_scalar_input_returns_unchanged():
    """如果整個 input 不是 dict/list 而是純字串 → 不變"""
    assert redact_pii("plain_string") == "plain_string"
    assert redact_pii(42) == 42


def test_uppercase_key_redacted():
    """key 大小寫不敏感"""
    out = redact_pii({"Phone_Number": "X"})
    assert out == {"Phone_Number": _FILTERED}
