"""
敏感欄位遮蔽工具函式（銀行帳號、身分證號等）
"""
from typing import Optional


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
