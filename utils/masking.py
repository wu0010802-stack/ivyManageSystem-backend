"""
敏感欄位遮蔽工具函式（銀行帳號、身分證號、電話等）
"""

import re
from typing import Optional


def mask_phone(phone: Optional[str]) -> Optional[str]:
    """遮蔽電話號碼，保留前 4 碼與末 3 碼，中段以 *** 取代。

    - 台灣手機 0912345678 / 0912-345-678 / 0912 345 678 → 0912-***-678
    - 其他長度 fallback：保留前 4 末 3，中間 ***
    - 長度過短（≤ 7 碼）→ 全部以 * 取代
    - None / 空字串 → None
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 7:
        return "*" * len(digits) if digits else None
    return f"{digits[:4]}-***-{digits[-3:]}"


def mask_bank_account(account: Optional[str]) -> Optional[str]:
    """遮蔽銀行帳號，僅保留末 4 碼（如 ****1234）。
    輸入為 None 或空字串時回傳 None。
    """
    if not account:
        return None
    return f"****{account[-4:]}" if len(account) > 4 else "****"


def mask_id_number(id_number: Optional[str]) -> Optional[str]:
    """遮蔽身分證號，僅保留前 3 碼（如 A12******）。
    輸入為 None 或空字串時回傳 None。
    """
    if not id_number:
        return None
    return id_number[:3] + "*" * (len(id_number) - 3) if len(id_number) > 3 else "***"
