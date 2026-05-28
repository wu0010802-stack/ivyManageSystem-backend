"""P0d-1 兒童醫療欄位加密 helper 測試。

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §4.1
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _set_test_key(monkeypatch):
    """每個 test 用同一個 Fernet key（generate 一次 cache）；清 lru_cache。"""
    test_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MEDICAL_FIELD_ENCRYPTION_KEY", test_key)

    # 清 settings cache + helper cache
    from config import get_settings
    from utils import medical_encryption

    get_settings.cache_clear()
    medical_encryption._get_fernet.cache_clear()

    yield

    get_settings.cache_clear()
    medical_encryption._get_fernet.cache_clear()


# ── encrypt / decrypt roundtrip ──


def test_encrypt_decrypt_roundtrip_chinese():
    from utils.medical_encryption import decrypt_medical, encrypt_medical

    plain = "花粉過敏，發作時打噴嚏 + 流鼻水"
    ct = encrypt_medical(plain)
    assert ct != plain
    assert decrypt_medical(ct) == plain


def test_encrypt_decrypt_roundtrip_english():
    from utils.medical_encryption import decrypt_medical, encrypt_medical

    plain = "amoxicillin 250mg, 3 times daily"
    ct = encrypt_medical(plain)
    assert decrypt_medical(ct) == plain


def test_encrypt_different_iv_each_call():
    """Fernet 每次 encrypt 用新 IV/timestamp → 同明文兩次 ciphertext 不同（語意安全）"""
    from utils.medical_encryption import encrypt_medical

    plain = "test"
    ct1 = encrypt_medical(plain)
    ct2 = encrypt_medical(plain)
    assert ct1 != ct2  # IV 不同


def test_ciphertext_is_ascii_safe():
    """輸出必為 ASCII-safe URL-safe base64（可存 Text column）"""
    from utils.medical_encryption import encrypt_medical

    plain = "中文測試"
    ct = encrypt_medical(plain)
    assert ct is not None
    ct.encode("ascii")  # 不應 raise


# ── None / empty 處理 ──


def test_encrypt_none_returns_none():
    from utils.medical_encryption import encrypt_medical

    assert encrypt_medical(None) is None


def test_encrypt_empty_returns_empty():
    from utils.medical_encryption import encrypt_medical

    assert encrypt_medical("") == ""


def test_decrypt_none_returns_none():
    from utils.medical_encryption import decrypt_medical

    assert decrypt_medical(None) is None


def test_decrypt_empty_returns_empty():
    from utils.medical_encryption import decrypt_medical

    assert decrypt_medical("") == ""


# ── Legacy plaintext 兼容（migration window 關鍵）──


def test_decrypt_legacy_plaintext_returns_unchanged():
    """非 Fernet token 原樣回傳，讓 ORM 切 TypeDecorator 後仍可讀既有資料。"""
    from utils.medical_encryption import decrypt_medical

    legacy = "舊的明文過敏資訊"
    assert decrypt_medical(legacy) == legacy


def test_decrypt_random_bytes_returns_unchanged():
    from utils.medical_encryption import decrypt_medical

    random_input = "not a valid fernet token at all"
    assert decrypt_medical(random_input) == random_input


# ── is_encrypted 判斷（給 backfill script 用）──


def test_is_encrypted_true_for_real_ciphertext():
    from utils.medical_encryption import encrypt_medical, is_encrypted

    ct = encrypt_medical("test")
    assert is_encrypted(ct) is True


def test_is_encrypted_false_for_plaintext():
    from utils.medical_encryption import is_encrypted

    assert is_encrypted("花粉過敏") is False


def test_is_encrypted_false_for_none_empty():
    from utils.medical_encryption import is_encrypted

    assert is_encrypted(None) is False
    assert is_encrypted("") is False


# ── Key 未設定 ──


def test_encrypt_raises_when_key_missing(monkeypatch):
    monkeypatch.delenv("MEDICAL_FIELD_ENCRYPTION_KEY", raising=False)
    from config import get_settings
    from utils import medical_encryption

    get_settings.cache_clear()
    medical_encryption._get_fernet.cache_clear()

    with pytest.raises(RuntimeError, match="MEDICAL_FIELD_ENCRYPTION_KEY"):
        medical_encryption.encrypt_medical("test")


# ── EncryptedText TypeDecorator basic ──


def test_encrypted_text_type_bind_and_result():
    """TypeDecorator process_bind_param 加密、process_result_value 解密。"""
    from utils.medical_field_type import EncryptedText

    et = EncryptedText()
    bind = et.process_bind_param("過敏資訊", dialect=None)
    assert bind != "過敏資訊"  # encrypted
    assert et.process_result_value(bind, dialect=None) == "過敏資訊"


def test_encrypted_text_handles_none():
    from utils.medical_field_type import EncryptedText

    et = EncryptedText()
    assert et.process_bind_param(None, dialect=None) is None
    assert et.process_result_value(None, dialect=None) is None


def test_encrypted_text_legacy_plaintext_passthrough():
    """既有 plaintext column 切到 EncryptedText 後仍能讀。"""
    from utils.medical_field_type import EncryptedText

    et = EncryptedText()
    assert et.process_result_value("舊的明文過敏", dialect=None) == "舊的明文過敏"
