"""utils/medical_encryption.py — application-level 對稱加密 for 兒童醫療欄位。

P0d 法規/個資 sprint 第四件 Phase 1：個資法 §6 特種個資（醫療/健康檢查）
「需法律明文 / 當事人書面同意」+ §47 罰責 5-50 萬。

Backend: cryptography.Fernet (AES-128-CBC + HMAC-SHA256，URL-safe base64)
Key: 從 env MEDICAL_FIELD_ENCRYPTION_KEY (base64 32 bytes)
     用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成

Phase 1 範圍（本 PR）:
- encrypt_medical / decrypt_medical 純函式
- 不動既有 model column type（待 Phase 2 PR 配 Alembic migration + backfill）
- Migration window 兼容：decrypt_medical 對非 Fernet token 原樣回傳

Phase 2 (follow-up):
- SQLAlchemy EncryptedText TypeDecorator
- 改 Student.allergy / medication / special_needs Column 型別
- 改 StudentContactBookEntry.temperature_c
- backfill script

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §3.1
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """singleton Fernet。若 env 未設則 raise RuntimeError。"""
    key = get_settings().medical.field_encryption_key
    if not key:
        raise RuntimeError("MEDICAL_FIELD_ENCRYPTION_KEY 未設定；prod 啟動前必設")
    return Fernet(key.encode())


def encrypt_medical(plaintext: str | None) -> str | None:
    """加密。None / empty 直接回傳（避免無意義 ciphertext）。

    輸出為 ASCII-safe URL-safe base64 string，可存進 Text column。
    每次 encrypt 結果不同（Fernet 含 IV/timestamp），確保語意安全。
    """
    if plaintext is None or plaintext == "":
        return plaintext
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_medical(ciphertext: str | None) -> str | None:
    """解密。None / empty 直接回傳。

    Migration window 兼容：輸入若不是 valid Fernet token（legacy plaintext，
    含中文等非 ASCII 字元），原樣回傳 — 讓 ORM 層在 Phase 2 切 TypeDecorator
    後仍可讀既有資料，避免「先加密 column 但既有資料無法 decrypt」災難。

    Fernet token 必為 ASCII-safe URL-safe base64，所以無法 ASCII encode 的
    輸入直接視為 legacy plaintext 回傳。
    """
    if ciphertext is None or ciphertext == "":
        return ciphertext
    try:
        ct_bytes = ciphertext.encode("ascii")
    except UnicodeEncodeError:
        # Legacy plaintext 含非 ASCII（如中文）→ 不可能是 Fernet token，原樣回
        return ciphertext
    try:
        return _get_fernet().decrypt(ct_bytes).decode("utf-8")
    except InvalidToken:
        # ASCII 但格式不對 → legacy plaintext during migration window
        return ciphertext
    except Exception as exc:
        logger.error("medical_encryption decrypt 失敗: %s", exc, exc_info=True)
        raise


def is_encrypted(value: str | None) -> bool:
    """判斷 value 是否為 valid Fernet ciphertext（給 backfill script 判斷用）。"""
    if value is None or value == "":
        return False
    try:
        ct_bytes = value.encode("ascii")
    except UnicodeEncodeError:
        return False  # 非 ASCII 字元 → 必是 legacy plaintext
    try:
        _get_fernet().decrypt(ct_bytes)
        return True
    except (InvalidToken, ValueError):
        return False
