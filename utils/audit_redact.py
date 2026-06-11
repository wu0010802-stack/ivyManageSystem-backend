"""utils/audit_redact.py — audit_logs.changes PII 遮罩。

P0b 法規/個資 sprint 第二件：避免 DB dump 外洩時 audit 表比主表更危險。

設計：
- 複用 utils/sentry_init._PII_KEY_SUBSTRINGS + _PII_KEY_EXEMPT_SUBSTRINGS
  （CLAUDE.md #8 / 單一資料源）
- 額外維護 _AUDIT_VALUE_KEEP_SUBSTRINGS：amount 類欄位 value 保留，
  因為 audit 需要金額變化軌跡（誰改了 50000 → 80000）才有稽核價值
- 命中遮罩規則：key 含 PII substring AND 不在 audit-keep AND 不在 sentry-exempt
- 命中後保留 key，value 替換為 [Filtered]
- 純函式 / 無 I/O / O(n) 遞迴 dict + list

Refs: spec docs/superpowers/specs/2026-05-28-audit-pii-redact-retention-design.md
"""

from __future__ import annotations

import re

from utils.sentry_init import _PII_KEY_EXEMPT_SUBSTRINGS, _PII_KEY_SUBSTRINGS

_FILTERED = "[Filtered]"

# Finding H：自由文字（如 audit summary）的強識別子樣式。summary 由各端點自由組
# 字串，redact_pii（key-based）對它無效。這裡只遮「高辨識度且不會與操作 ID 衝突」
# 的識別子——刻意不遮純數字 user_id/employee_id 與姓名（admin-only 稽核面需可讀）。
_TW_ID_RE = re.compile(r"\b[A-Za-z][12A-Da-d]\d{8}\b")  # 身分證 / 居留證
_MOBILE_RE = re.compile(r"\b09\d{8}\b")  # 手機
_LANDLINE_RE = re.compile(r"\b0\d{1,2}-\d{6,8}\b")  # 市話（帶 dash）


def redact_pii_text(text):
    """遮罩自由文字中的強識別子（身分證/居留證、手機、帶 dash 市話）。

    非字串（None/其他）原樣回傳。只遮樣式明確的識別子，避免誤遮操作 ID/金額/姓名。
    """
    if not isinstance(text, str) or not text:
        return text
    text = _TW_ID_RE.sub(_FILTERED, text)
    text = _MOBILE_RE.sub(_FILTERED, text)
    text = _LANDLINE_RE.sub(_FILTERED, text)
    return text


# Audit 例外：value 保留欄位。兩類：
# 1. amount 類（金流稽核需保留金額變化軌跡）
# 2. metadata flag 類（如 is_full_bank_account：布林旗標標示「此筆匯出是否含完整 PII」，
#    需保留 True/False 才能在 audit 中篩選「曾匯出完整 PII」事件，本身不是 PII 值）
# 注意：不含 bank_account / card_no / id_number — 那些是識別子不是 amount 也不是 flag
_AUDIT_VALUE_KEEP_SUBSTRINGS: frozenset[str] = frozenset(
    {
        # amount 類
        "salary_amount",
        "bonus_amount",
        "fee_amount",
        "total_amount",
        "gross_salary",
        "net_salary",
        "deduction_amount",
        "payment_amount",
        "overtime_amount",
        "leave_payout_amount",
        "insured_amount",
        "amount_due",
        "amount_paid",
        # metadata flag 類（is_full_* 開頭表示「是否含完整 X」布林旗標，不是實際 X 值）
        "is_full_bank_account",
        "is_full_id_number",
        "is_full_phone",
    }
)


def _should_redact(key: str) -> bool:
    """key 應否遮罩。

    順序：sentry-exempt > audit-keep > sentry-denylist。
    Exempt 命中即不遮（保留系統 metadata 欄位如 ip_address / health_check）；
    audit-keep 命中即不遮（保留金流 amount 數字）；
    都不命中時看是否在 sentry-denylist。
    """
    key_lower = key.lower()
    if any(s in key_lower for s in _PII_KEY_EXEMPT_SUBSTRINGS):
        return False
    if any(s in key_lower for s in _AUDIT_VALUE_KEEP_SUBSTRINGS):
        return False
    return any(s in key_lower for s in _PII_KEY_SUBSTRINGS)


def redact_pii(changes):
    """遞迴遮罩 changes。命中 key 的 value 替換為 [Filtered]，保留 key。

    支援結構：
      - dict: 對 value 遞迴；若 key 命中 redact 規則整個 value 遮（不再遞迴 nested）
      - list: 對每個 item 遞迴
      - 純值 (str / int / None / bool / float): 直接回傳

    Edge cases:
      - changes 為 None: 回 None
      - dict.value 為 dict/list 且 key 不命中: 遞迴
      - dict.value 為 dict/list 且 key 命中: 整個 value 變 [Filtered]（不洩漏 nested PII）

    回傳型別與輸入一致（dict in → dict out / list in → list out / None → None）。
    """
    if changes is None:
        return None
    if isinstance(changes, list):
        return [redact_pii(item) for item in changes]
    if not isinstance(changes, dict):
        return changes

    result: dict = {}
    for key, value in changes.items():
        if _should_redact(key):
            # 整個 value 遮，不再遞迴（避免 {"phone": {"before":..., "after":...}} 洩漏）
            result[key] = _FILTERED
        elif isinstance(value, (dict, list)):
            result[key] = redact_pii(value)
        else:
            result[key] = value
    return result
